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
from .output_truth import OutputTruthOracle
from .provider_ledger import ProviderLedgerOracle
from .revision import RevisionOracle
from .schema import OracleResult, failing, passing, skipping
from .tool_scope import ToolScopeOracle

__all__ = [
    "BROWSER_EVIDENCE_ORACLES",
    "BrowserWSOracle",
    "CleanupOracle",
    "ContractOracle",
    "EventChainOracle",
    "ExportDownloadOracle",
    "HarnessValidityOracle",
    "LifecycleOracle",
    "OracleResult",
    "OutputTruthOracle",
    "PreviewOwnershipOracle",
    "ProviderLedgerOracle",
    "RevisionOracle",
    "ShowToUserOracle",
    "SidecarStopOracle",
    "ToolScopeOracle",
    "VerificationGateOracle",
    "failing",
    "passing",
    "skipping",
]
