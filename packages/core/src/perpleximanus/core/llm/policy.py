"""Overflow signal + the (DORMANT) overflow policy — llm-router-contract.md §5.

v1.2 LOBOTOMY: model selection is deterministic config assignment (config.py /
routing.py); there is no longer a local-vs-frontier decision to make at call
time. The five-rule `ThresholdOverflowPolicy` is therefore **dormant** — kept in
this file, commented out, as the documented revival path (contract v1.2
appendix). What stays LIVE is `OverflowSignal`: the loop still constructs it to
carry runtime signals (difficulty, tool-error streak, confidence) as ADVISORY
metadata onto the call, even though nothing routes on it anymore. Keeping the
type live means the loop's seam (`agent.py`) is unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .types import Difficulty, Requirement


class OverflowSignal(BaseModel):
    """Runtime inputs once consumed by the overflow policy; now ADVISORY only.

    The loop still builds this to annotate a call with its runtime view
    (difficulty hint, tool-error streak, last local confidence). In v1.2 the
    router does NOT read it to choose a model — selection is config-driven — but
    the type stays live so the loop seam is untouched and the signals remain
    available for observability / a future revival of the policy below."""

    model_config = ConfigDict(frozen=True)
    difficulty: Difficulty
    consecutive_tool_errors: int = 0  # from the loop (stuck-adjacent)
    last_local_confidence: float | None = None  # if the caller estimates one
    local_attempts_failed: int = 0  # local calls already failed this step
    requires: frozenset[Requirement] = frozenset()


# === INTELLIGENT ROUTING (DORMANT) — five-rule overflow policy (contract §5) ===
# The policy below decided local-vs-overflow by ANY of five rules firing
# (capability gap / hard step / local failure / stuck recovery / low confidence),
# with thresholds from config and cost governance applied by the router around
# it. v1.2 demoted selection to deterministic assignment, so this whole layer is
# intentionally dormant. It is preserved verbatim (commented) so the revival path
# is a diff, not a rewrite. To revive: uncomment, restore the threshold/`roles`
# fields on RouterConfig (config.py), re-add the policy params + intelligent
# `_resolve` body in routing.py, and re-export `OverflowPolicy`/
# `ThresholdOverflowPolicy` from `__init__.py`.
#
# from typing import Literal, Protocol, runtime_checkable
# from .config import RouterConfig
# from .types import CapabilityProfile, ModelRole
#
# # Discretionary triggers suppressed when the soft budget cap is exceeded (§5.2).
# DISCRETIONARY_TRIGGERS = frozenset({"hard_step", "low_confidence"})
#
# # Roles that default to local-only (never overflow) unless config opts in (§5.1).
# _LOCAL_ONLY_ROLES = frozenset(
#     {ModelRole.RAG_ANSWERER, ModelRole.QUERY_REWRITER, ModelRole.SUMMARIZER}
# )
#
#
# @runtime_checkable
# class OverflowPolicy(Protocol):
#     """[CONTRACT] Decides local vs overflow and may request escalation.
#     Returns (path, triggers)."""
#
#     def decide(
#         self, profile: CapabilityProfile, signal: OverflowSignal, *, config: RouterConfig
#     ) -> tuple[Literal["local", "overflow"], list[str]]: ...
#
#
# class ThresholdOverflowPolicy:
#     """[CONTRACT] The default policy: route to overflow if ANY rule fires, else
#     local. Thresholds come from config (none hardcoded)."""
#
#     def decide(
#         self, profile: CapabilityProfile, signal: OverflowSignal, *, config: RouterConfig
#     ) -> tuple[Literal["local", "overflow"], list[str]]:
#         triggers: list[str] = []
#         role = profile.role
#         routing = config.roles.get(role)
#         role_overflow_eligible = bool(routing and routing.overflow_eligible)
#
#         # Rule 1 — capability gap: a required capability the local primary lacks
#         # but the overflow model has. Fires regardless of role eligibility
#         # (it is a hard necessity), provided an overflow model exists.
#         if signal.requires and routing is not None:
#             local = config.models.get(routing.primary)
#             overflow = config.models.get(routing.overflow) if routing.overflow else None
#             local_caps = local.capabilities if local else frozenset()
#             overflow_caps = overflow.capabilities if overflow else frozenset()
#             gap = {r for r in signal.requires if r not in local_caps}
#             if gap and gap.issubset(overflow_caps):
#                 triggers.append("capability_gap")
#
#         # Roles that are local-only never take the discretionary/eligible rules.
#         eligible = role_overflow_eligible and role not in _LOCAL_ONLY_ROLES
#
#         if eligible:
#             # Rule 2 — hard step on a routing-eligible role (discretionary).
#             if signal.difficulty == Difficulty.HARD:
#                 triggers.append("hard_step")
#             # Rule 3 — local failure escalation.
#             if signal.local_attempts_failed >= config.local_retry_before_overflow:
#                 triggers.append("local_failed")
#             # Rule 4 — stuck recovery (hand a struggling step to a stronger model).
#             if signal.consecutive_tool_errors >= config.errors_before_overflow:
#                 triggers.append("stuck_recovery")
#             # Rule 5 — low confidence (discretionary).
#             if (
#                 signal.last_local_confidence is not None
#                 and signal.last_local_confidence < config.confidence_floor
#             ):
#                 triggers.append("low_confidence")
#
#         path: Literal["local", "overflow"] = "overflow" if triggers else "local"
#         return path, triggers
# === END DORMANT ==============================================================
