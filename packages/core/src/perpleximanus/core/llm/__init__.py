"""perpleximanus.core.llm — the LLM Router boundary (llm-router-contract.md).

Provider-neutral access to language models: callers declare a CapabilityProfile
(role + difficulty + requirements), never a model name; the router maps the ROLE
to a concrete model by DETERMINISTIC config assignment (v1.2 — settings
assignment / per-conversation model pill), injects the per-family/per-mode
prompt, and returns a provider-neutral CompletionResponse with a RoutingDecision
attached. The intelligent overflow policy is dormant (see policy.py); only
`OverflowSignal` stays live as advisory metadata.

Also delivers the two functions the event/state contract was waiting on:
`is_context_window_exceeded` (errors) and the `Summarizer` (RouterSummarizer).
"""

from __future__ import annotations

from .config import (
    EncodersSettings,
    ExtractionSettings,
    ModelEntry,
    ProjectStorageSettings,
    RoleRouting,
    RouterConfig,
    SandboxSettings,
    SearchSettings,
    default_config,
)
from .config_store import ConfigStore
from .errors import (
    BudgetExceeded,
    LLMAuthError,
    LLMContentFiltered,
    LLMContextWindowExceeded,
    LLMError,
    LLMTransientError,
    NoEligibleModel,
    is_context_window_exceeded,
)
from .nli import Entailment, NLIVerifier, StubNLIVerifier

# v1.2: OverflowPolicy / ThresholdOverflowPolicy are DORMANT (policy.py). Only
# OverflowSignal stays live (advisory metadata carried by the loop).
from .policy import OverflowSignal
from .prompts import (
    DriverPrompts,
    PromptProvider,
    StaticPromptProvider,
    default_mode_for_role,
    derive_family,
)
from .provider import ModelProvider
from .routing import (
    CallContext,
    CostTracker,
    DefaultLLMRouter,
    InMemoryRoutingSink,
    LLMRouter,
    NullRoutingSink,
    RoutingSink,
)
from .secrets import SecretBox, SecretStore
from .summarizer import RouterSummarizer
from .types import (
    CapabilityProfile,
    CompletionRequest,
    CompletionResponse,
    Difficulty,
    ModelRole,
    OperatingMode,
    ProposedToolCall,
    Requirement,
    RoutingDecision,
    StreamChunk,
    TokenUsage,
    ToolSpec,
)

__all__ = [
    "BudgetExceeded",
    "CallContext",
    "CapabilityProfile",
    "CompletionRequest",
    "CompletionResponse",
    "CostTracker",
    "DefaultLLMRouter",
    "Difficulty",
    "Entailment",
    "InMemoryRoutingSink",
    "LLMAuthError",
    "LLMContentFiltered",
    "LLMContextWindowExceeded",
    "LLMError",
    "LLMRouter",
    "LLMTransientError",
    "ModelEntry",
    "ModelProvider",
    "ModelRole",
    "NLIVerifier",
    "NoEligibleModel",
    "NullRoutingSink",
    "OperatingMode",
    "OverflowSignal",
    "DriverPrompts",
    "PromptProvider",
    "ProposedToolCall",
    "Requirement",
    "RoleRouting",
    "RouterConfig",
    "ProjectStorageSettings",
    "SandboxSettings",
    "EncodersSettings",
    "ExtractionSettings",
    "SearchSettings",
    "RouterSummarizer",
    "RoutingDecision",
    "RoutingSink",
    "StaticPromptProvider",
    "StreamChunk",
    "StubNLIVerifier",
    "TokenUsage",
    "ToolSpec",
    "default_config",
    "ConfigStore",
    "SecretBox",
    "SecretStore",
    "default_mode_for_role",
    "derive_family",
    "is_context_window_exceeded",
]
