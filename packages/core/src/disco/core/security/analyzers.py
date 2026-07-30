"""Analyzer strategies — security-analyzer-contract.md §4.

Compatibility facade: shell substitution parsing, shell deny parsing, and
tool-risk scoring now live in :mod:`shell_substitution`, :mod:`shell_deny`, and
:mod:`tool_risk`.  This module re-exports the public names and keeps the
analyzer classes so existing import paths remain unchanged.

- `RuleBasedAnalyzer` (§4.1): fast, deterministic, no model call; the always-on
  floor. Parses the shell tool's raw command for known-dangerous shapes and
  factors other tools' base_risk + argument shape.
- `LLMBasedAnalyzer` (§4.2): contextual judgement via an injected sync `scorer`;
  ADVISORY + ADDITIVE only (it can raise caution via the ensemble, never lower).
- `EnsembleAnalyzer` (§4.3): composes analyzers, returns the MOST cautious verdict
  via `max_risk`; runs the LLM only when the rule-based result warrants it.

The interface (§3) is the contract; the rule list / prompt are [INTERIOR] and
tunable. The strategy roles + ensemble behavior are contractual.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ..events import ActionEvent, SecurityRisk
from .assessment import RiskAssessment
from .risk import at_or_above, max_risk
from .shell_deny import hard_deny_reason as hard_deny_reason
from .tool_risk import score_other_with_base, score_shell

# A sync risk scorer (the LLM analyzer's injected dependency). The real one
# bridges to the router; tests inject a fake. Kept sync so the loop's assess()
# boundary stays sync (contract §3 [VERIFY]); async router scoring is deferred.
Scorer = Callable[[ActionEvent], RiskAssessment]

_U = SecurityRisk.UNKNOWN


# ---- the rule-based analyzer ------------------------------------------------


class RuleBasedAnalyzer:
    """[CONTRACT role; INTERIOR rules] Fast, deterministic, no model call — the
    always-on floor under every action. For the `shell` tool it parses the raw
    command; for other tools it factors the injected `base_risk` (the tool/sandbox
    layer's static hint, passed in because `core` cannot import `tools`) and the
    argument shape. Sync + total: any internal error returns UNKNOWN (cautious),
    never raises."""

    name = "rule_based"

    def __init__(self, base_risk_by_tool: Mapping[str, SecurityRisk] | None = None) -> None:
        self._base = dict(base_risk_by_tool or {})

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            risk, rationale = self._score(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution, never crash the gate
            return RiskAssessment(
                risk=_U,
                rationale=f"rule analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        return RiskAssessment(
            risk=risk,
            rationale=rationale,
            analyzer=self.name,
            self_assessed=action.self_assessed_risk,
        )

    def _score(self, action: ActionEvent) -> tuple[SecurityRisk, str]:
        tc = action.tool_call
        if tc.tool_name in {"shell", "shell_exec"}:
            # The tool contract guarantees the shell tools surface their raw command.
            return score_shell(str(tc.arguments.get("command", "")))
        return score_other_with_base(tc.tool_name, tc.arguments, self._base)


# ---- the LLM-based analyzer (advisory, additive) ----------------------------


class LLMBasedAnalyzer:
    """[CONTRACT role; INTERIOR prompt] Catches novel/contextual risk a rule list
    misses, via an injected sync `scorer`. ADVISORY + ADDITIVE: it only ever
    contributes to the ensemble's `max_risk`, so it can raise caution but never
    lower what the rule-based analyzer flagged (injection-resistance, §4.2). Sync
    + total: scorer errors contribute UNKNOWN, never raise."""

    name = "llm_based"

    def __init__(self, scorer: Scorer) -> None:
        self._scorer = scorer

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        try:
            result = self._scorer(action)
        except Exception as exc:  # noqa: BLE001 — fail toward caution
            return RiskAssessment(
                risk=_U,
                rationale=f"llm analyzer error: {type(exc).__name__}",
                analyzer=self.name,
                self_assessed=action.self_assessed_risk,
            )
        # Normalize: ensure the contributor is attributed to this analyzer.
        return result.model_copy(update={"analyzer": result.analyzer or self.name})


# ---- the ensemble -----------------------------------------------------------


class EnsembleAnalyzer:
    """[CONTRACT] Composes analyzers and returns the MOST cautious verdict via
    `max_risk` (§4.3). The rule-based analyzer runs on EVERY action (the floor);
    the LLM analyzer runs only when the rule-based result warrants it (at/above a
    configurable trigger, or UNKNOWN) — so trivial reads aren't taxed with a model
    call. No analyzer can relax another's caution."""

    name = "ensemble"

    def __init__(
        self,
        rule_based: RuleBasedAnalyzer,
        llm: LLMBasedAnalyzer | None = None,
        *,
        llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
    ) -> None:
        self._rule_based = rule_based
        self._llm = llm
        self._llm_trigger = llm_trigger

    def assess(self, action: ActionEvent) -> SecurityRisk:
        return self.assess_detailed(action).risk

    def _warrants_llm(self, rule_risk: SecurityRisk) -> bool:
        # Run the (expensive) LLM only for non-trivial actions: at/above the
        # trigger, or when the rule-based analyzer was itself unsure.
        return rule_risk == _U or at_or_above(rule_risk, self._llm_trigger)

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment:
        contributors: list[RiskAssessment] = []
        rb = self._rule_based.assess_detailed(action)  # always
        contributors.append(rb)
        combined = rb.risk

        if self._llm is not None and self._warrants_llm(rb.risk):
            llm = self._llm.assess_detailed(action)
            contributors.append(llm)
            combined = max_risk(combined, llm.risk)  # additive caution only

        ran = [c.analyzer for c in contributors]
        return RiskAssessment(
            risk=combined,
            rationale=f"ensemble of {', '.join(ran)} → {combined.value} (most cautious)",
            analyzer=self.name,
            contributors=contributors,
            self_assessed=action.self_assessed_risk,
        )


def build_default_analyzer(
    base_risk_by_tool: Mapping[str, SecurityRisk] | None = None,
    llm_scorer: Scorer | None = None,
    *,
    llm_trigger: SecurityRisk = SecurityRisk.MEDIUM,
) -> EnsembleAnalyzer:
    """The default ensemble: rule-based (always) + optional LLM (when warranted).
    `base_risk_by_tool` is injected by the wiring layer (which can see `tools`)."""
    rule = RuleBasedAnalyzer(base_risk_by_tool)
    llm = LLMBasedAnalyzer(llm_scorer) if llm_scorer is not None else None
    return EnsembleAnalyzer(rule, llm, llm_trigger=llm_trigger)
