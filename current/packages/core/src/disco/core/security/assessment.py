"""The RiskAssessment record + the SecurityAnalyzer protocol — contract §3.

`RiskAssessment` is the full result of scoring (richer than the bare
`SecurityRisk` the loop boundary returns) — it carries the rationale, the
producing analyzer, the per-analyzer breakdown for an ensemble, and the agent's
self-assessment, for audit (§7) and the confirmation UI.

`SecurityAnalyzer` here is the RICHER protocol (adds `name` + `assess_detailed`)
that this subsystem's analyzers implement; it is a structural superset of the
loop's minimal `SecurityAnalyzer` (which needs only `assess`), so any analyzer
here also satisfies the loop boundary.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..events import ActionEvent, SecurityRisk


class RiskAssessment(BaseModel):
    """[CONTRACT] The full result of scoring. `assess()` returns the bare
    `SecurityRisk`; `assess_detailed()` returns this."""

    model_config = ConfigDict(frozen=True)
    risk: SecurityRisk
    rationale: str  # human-readable why (for audit/UI)
    analyzer: str  # which analyzer produced this
    # per-analyzer breakdown when this is an ensemble result (§4)
    contributors: list[RiskAssessment] = Field(default_factory=list)
    # the agent's own self-assessment, carried through for the audit trail
    self_assessed: SecurityRisk = SecurityRisk.UNKNOWN


@runtime_checkable
class SecurityAnalyzer(Protocol):
    """[CONTRACT — fulfills agent-loop contract §3] Scores a proposed action's
    risk BEFORE execution. `assess()` is the loop boundary (sync, total, never
    raises); `assess_detailed()` feeds audit/UI. May refine (override) the
    action's `self_assessed_risk` — its returned score is authoritative."""

    name: str

    def assess(self, action: ActionEvent) -> SecurityRisk: ...

    def assess_detailed(self, action: ActionEvent) -> RiskAssessment: ...
