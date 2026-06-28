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
    EditContract,
    ExportContract,
    ToolPack,
    VerificationContract,
    VerificationLevel,
)
from .registry import BuildContractRegistry

__all__ = [
    "ArtifactContract",
    "BuildContract",
    "BuildContractRegistry",
    "ContractKind",
    "EditContract",
    "ExportContract",
    "ToolPack",
    "VerificationContract",
    "VerificationLevel",
]
