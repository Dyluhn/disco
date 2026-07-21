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
    TypedVerifierVerdict,
    VerifierContextSeed,
    VerifierJudge,
    VerifierScreenshot,
)
from .engine import AgentLoop, AgentViewSuperseded
from .finish import host_verify_authoritative_enabled
from .policies import (
    AlwaysConfirm,
    BlastRadiusConfirm,
    ConfirmRisky,
    NeverConfirm,
    NullSecurityAnalyzer,
    SelfAssessedAnalyzer,
)
from .progress import (
    ProgressKind,
    ProgressState,
    RecoveryLeasePhase,
    RecoveryLeaseState,
    recovery_lease_id,
    reduce_progress,
)
from .stuck import StuckDetector, StuckThresholds

__all__ = [
    "Agent",
    "AgentLoop",
    "AgentViewSuperseded",
    "AgentStep",
    "AlwaysConfirm",
    "BlastRadiusConfirm",
    "ConfirmRisky",
    "ConfirmationPolicy",
    "HostVerificationDeliverable",
    "HostVerifier",
    "host_verify_authoritative_enabled",
    "NeverConfirm",
    "NullSecurityAnalyzer",
    "ProgressKind",
    "ProgressState",
    "BuildAgent",
    "ResearchAgent",
    "RouterAgent",
    "RecoveryLeasePhase",
    "RecoveryLeaseState",
    "SecurityAnalyzer",
    "SelfAssessedAnalyzer",
    "StopHook",
    "StuckDetector",
    "StuckThresholds",
    "TypedVerifierVerdict",
    "ToolExecutor",
    "VerifierContextSeed",
    "VerifierJudge",
    "VerifierScreenshot",
    "recovery_lease_id",
    "reduce_progress",
]
