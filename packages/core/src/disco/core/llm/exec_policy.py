"""ModelExecutionPolicy — the SINGLE source of truth for model-tier execution.

Background (2026-06-20 assist-pipeline refactor): the weak-model "assist" gate used
to be a bare boolean smeared across runtime, the LLM request, every loop collaborator,
and the tool context — AND the tool surface was gated by a SECOND, independent
classifier (the ANCHORED_EDIT capability). The two never reconciled, so a capable
local model could get the small-model prompt + every compensation but the full capable
toolset, and a cached loop's frozen boolean could go stale against the live UI badge.

This object replaces that. It is resolved ONCE per conversation (`resolve_policy`),
frozen, and threaded immutably into the loop, executor, tool scope, prompt selection,
and the LLM request — so all consumers read the SAME decision. The `standard` tier is a
NO-OP: every weak-model gate is `if policy.assist`, so a standard-tier run is identical
to the pre-assist code path. The tier and the anchored-edit capability now live on ONE
object, so the tool surface and the compensations can no longer disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["standard", "weak"]


@dataclass(frozen=True)
class ModelExecutionPolicy:
    """How a conversation's driver model should be executed. Immutable; resolve once."""

    # "weak" enables the assist compensations (F1/F3/F4/F5/F6/F7/F8/F9/HS-01/HS-03 and
    # the small-model prompt). "standard" is the no-op default — provably identical to
    # the pre-assist path.
    tier: Tier = "standard"
    # Whether the model reliably performs anchored (search/replace) edits. A CAPABILITY,
    # distinct from tier: it governs only whether the line-anchored edit tool is
    # advertised. Carried here so the tool surface is decided by the SAME object as the
    # compensations (the former two-classifiers bug).
    anchored_edit: bool = True

    @property
    def assist(self) -> bool:
        """The legacy weak-model gate. Every `if self._assist:` compensation reads this,
        so the standard tier (assist=False) is byte-identical to pre-assist."""
        return self.tier == "weak"

    @property
    def withheld_tools(self) -> frozenset[str]:
        """The single place the advertised tool surface is narrowed for this policy:
          • no anchored-edit capability → withhold `exact_replace`;
          • weak tier → ALSO withhold `update_plan_progress` (weak models get the honest
            NL plan + done-at-finish, never a per-step bookkeeping burden they drop).

        Legacy edit/progress tools that are no longer in AGENT_TOOLS do not need to be
        listed here; they stay registered for workflow/replay paths but are outside the
        agent scope before advertisement is considered.
        """
        out: set[str] = set()
        if not self.anchored_edit:
            out.add("exact_replace")  # CD-TOOLS-2 — anchored exact-match batch edit (capable tier)
        if self.tier == "weak":
            out.add("update_plan_progress")
        return frozenset(out)

    @classmethod
    def standard(cls) -> ModelExecutionPolicy:
        """The no-compensation default for a capable model with anchored edits
        and no weak-model compensations."""
        return cls(tier="standard", anchored_edit=True)


def resolve_policy(
    *,
    assist_override: bool | None,
    entry_tier: Tier | None,
    hosting_weak_default: bool,
    anchored_edit: bool,
) -> ModelExecutionPolicy:
    """Resolve the ONE policy for a conversation. Precedence for the tier:

      1. an explicit per-conversation assist toggle (the user flipped it) wins;
      2. else the model's explicit `tier` metadata (config.py ModelEntry.tier);
      3. else the hosting heuristic (local && !openrouter → weak) — pure back-compat.

    `anchored_edit` is the model's capability and is independent of the tier.
    """
    if assist_override is not None:
        tier: Tier = "weak" if assist_override else "standard"
    elif entry_tier is not None:
        tier = entry_tier
    else:
        tier = "weak" if hosting_weak_default else "standard"
    return ModelExecutionPolicy(tier=tier, anchored_edit=anchored_edit)
