"""Objective oracle layers (guidelines §10). Each emits structured OracleResult
facts — never prose — and never calls an LLM.

This foundation slice ships the deterministic, event-log-adjudicable layers:
HarnessValidity, Contract, EventChain, ToolScope, Revision, OutputTruth. The
UI/Transport/Runtime oracles belong to the later live/UI-runner slices.
"""

from __future__ import annotations

from .contract import ContractOracle
from .event_chain import EventChainOracle
from .harness_validity import HarnessValidityOracle
from .output_truth import OutputTruthOracle
from .revision import RevisionOracle
from .schema import OracleResult, failing, passing, skipping
from .tool_scope import ToolScopeOracle

__all__ = [
    "ContractOracle",
    "EventChainOracle",
    "HarnessValidityOracle",
    "OracleResult",
    "OutputTruthOracle",
    "RevisionOracle",
    "ToolScopeOracle",
    "failing",
    "passing",
    "skipping",
]
