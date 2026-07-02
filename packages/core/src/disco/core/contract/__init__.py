"""disco.core.contract — the Build Artifact Contract runtime (P2).

A Build run declares an ARTIFACT CONTRACT before execution: what kind of deliverable
it produces (a lead-gen app, a static site, a deck, …), the required files, the tool
packs it may use at each phase (bootstrap/edit/repair), how it is verified, and how it
is exported. The host owns truth; the model operates inside these rails.

CONTRACT-1 ships the pure, serializable domain models. The registry (CONTRACT-2) and
the Contract→ToolScope compiler (CONTRACT-3) consume them. No frontend/tool/loop
runtime dependencies — stable value objects only (same discipline as disco.core.context).
"""

from __future__ import annotations

from .models import (
    ArtifactContract,
    BuildContract,
    ContractKind,
    DeliveryMode,
    EditContract,
    ExportContract,
    ToolPack,
    VerificationContract,
    VerificationLevel,
    delivery_mode_for_kind,
    deliverable_kind_matches_contract,
)
from .enforce import (
    DANGEROUS_TOOLS,
    ContractScopeGuard,
    ScopeDecision,
    decide_tool_in_scope,
)
from .phase import BuildPhaseTracker
from .registry import BuildContractRegistry
from .scopes import PHASE_NEUTRAL_TOOLS, ContractToolScopes, Phase, compile_tool_scopes

__all__ = [
    "DANGEROUS_TOOLS",
    "ArtifactContract",
    "BuildContract",
    "BuildContractRegistry",
    "BuildPhaseTracker",
    "ContractKind",
    "ContractScopeGuard",
    "ContractToolScopes",
    "DeliveryMode",
    "EditContract",
    "ExportContract",
    "Phase",
    "PHASE_NEUTRAL_TOOLS",
    "ScopeDecision",
    "ToolPack",
    "VerificationContract",
    "VerificationLevel",
    "compile_tool_scopes",
    "decide_tool_in_scope",
    "deliverable_kind_matches_contract",
    "delivery_mode_for_kind",
]
