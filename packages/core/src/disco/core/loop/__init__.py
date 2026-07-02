"""disco.core.loop — the agent loop & orchestration (agent-loop-contract.md).

The control loop the product's reliability rests on: an explicit status state
machine over one-action-per-iteration steps, two-phase confirmation, stuck
detection, condensation wiring, and steering — all headless and transport-
agnostic. Binds the event/state spine and the LLM router together.
"""

from __future__ import annotations

from .agent import BuildAgent, ResearchAgent, RouterAgent
from .boundaries import (
    Agent,
    AgentStep,
    ConfirmationPolicy,
    HostVerificationDeliverable,
    HostVerifier,
    SecurityAnalyzer,
    StopHook,
    ToolExecutor,
)
from .engine import AgentLoop
from .policies import (
    AlwaysConfirm,
    BlastRadiusConfirm,
    ConfirmRisky,
    NeverConfirm,
    NullSecurityAnalyzer,
    SelfAssessedAnalyzer,
)
from .stuck import StuckDetector, StuckThresholds

__all__ = [
    "Agent",
    "AgentLoop",
    "AgentStep",
    "AlwaysConfirm",
    "BlastRadiusConfirm",
    "ConfirmRisky",
    "ConfirmationPolicy",
    "HostVerificationDeliverable",
    "HostVerifier",
    "NeverConfirm",
    "NullSecurityAnalyzer",
    "BuildAgent",
    "ResearchAgent",
    "RouterAgent",
    "SecurityAnalyzer",
    "SelfAssessedAnalyzer",
    "StopHook",
    "StuckDetector",
    "StuckThresholds",
    "ToolExecutor",
]
