"""Objective oracle layers (guidelines §10). Each emits structured OracleResult
facts — never prose — and never calls an LLM.

This foundation slice ships the deterministic, event-log-adjudicable layers:
HarnessValidity, Contract, EventChain, ToolScope, Revision, OutputTruth. The
UI/Transport/Runtime oracles belong to the later live/UI-runner slices.
"""

from __future__ import annotations

from .browser_evidence import (
    BROWSER_EVIDENCE_ORACLES,
    BrowserWSOracle,
    CleanupOracle,
    ExportDownloadOracle,
    LifecycleOracle,
    PreviewOwnershipOracle,
    ShowToUserOracle,
    SidecarStopOracle,
    VerificationGateOracle,
)
from .contract import ContractOracle
from .event_chain import EventChainOracle
from .harness_validity import HarnessValidityOracle
from .manual_edit_preservation import (
    CommentAnchorOracle,
    ManualEditPreservationOracle,
    ScreenLabelOracle,
)
from .output_truth import OutputTruthOracle
from .provider_ledger import ProviderLedgerOracle
from .revision import RevisionOracle
from .schema import OracleResult, failing, passing, skipping
from .targeted_edit import RewriteAvoidanceOracle, TargetedEditOracle
from .thrash import ThrashOracle
from .tool_scope import ToolScopeOracle

# P8D targeted/manual-edit oracles — wired into classify() SKIP-safe (each SKIPs without its
# product_evidence slice; the live producer is P1B-LIVE), mirroring BROWSER_EVIDENCE_ORACLES.
TARGETED_EDIT_ORACLES = (
    TargetedEditOracle,
    RewriteAvoidanceOracle,
    ManualEditPreservationOracle,
    CommentAnchorOracle,
    ScreenLabelOracle,
)

__all__ = [
    "BROWSER_EVIDENCE_ORACLES",
    "TARGETED_EDIT_ORACLES",
    "BrowserWSOracle",
    "CleanupOracle",
    "CommentAnchorOracle",
    "ContractOracle",
    "EventChainOracle",
    "ExportDownloadOracle",
    "HarnessValidityOracle",
    "LifecycleOracle",
    "ManualEditPreservationOracle",
    "OracleResult",
    "OutputTruthOracle",
    "PreviewOwnershipOracle",
    "ProviderLedgerOracle",
    "RevisionOracle",
    "RewriteAvoidanceOracle",
    "ScreenLabelOracle",
    "ShowToUserOracle",
    "SidecarStopOracle",
    "TargetedEditOracle",
    "ThrashOracle",
    "ToolScopeOracle",
    "VerificationGateOracle",
    "failing",
    "passing",
    "skipping",
]
