"""perpleximanus.core.security — action-risk scoring (security-analyzer-contract.md).

The action-risk gate: scores a proposed action's risk (rule-based + optional
LLM ensemble, most-cautious combination) so the loop's ConfirmationPolicy can
decide whether to stop for a human. Independent of the sandbox's hard isolation
(which holds regardless of any score here). The confirmation policies themselves
live in `core.loop` (they fulfill the loop boundary) and consume `at_or_above`.
"""

from __future__ import annotations

from .analyzers import (
    EnsembleAnalyzer,
    LLMBasedAnalyzer,
    RuleBasedAnalyzer,
    Scorer,
    build_default_analyzer,
)
from .assessment import RiskAssessment, SecurityAnalyzer
from .risk import at_or_above, max_risk

__all__ = [
    "EnsembleAnalyzer",
    "LLMBasedAnalyzer",
    "RiskAssessment",
    "RuleBasedAnalyzer",
    "Scorer",
    "SecurityAnalyzer",
    "at_or_above",
    "build_default_analyzer",
    "max_risk",
]
