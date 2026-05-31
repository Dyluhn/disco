"""perpleximanus.core.llm — the LLM Router boundary (llm-router-contract.md).

Capability-based, provider-neutral access to language models: callers declare a
CapabilityProfile (role + difficulty + requirements), never a model name; the
router maps that to a concrete model via config, applies the local/overflow
policy and cost governance, injects the per-family/per-mode prompt, and returns
a provider-neutral CompletionResponse with a RoutingDecision attached.

Also delivers the two functions the event/state contract was waiting on:
`is_context_window_exceeded` (errors) and the `Summarizer` (RouterSummarizer).
"""

from __future__ import annotations

from .config import ModelEntry, RoleRouting, RouterConfig, default_config
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
from .policy import OverflowPolicy, OverflowSignal, ThresholdOverflowPolicy
from .prompts import (
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
    "OverflowPolicy",
    "OverflowSignal",
    "PromptProvider",
    "ProposedToolCall",
    "Requirement",
    "RoleRouting",
    "RouterConfig",
    "RouterSummarizer",
    "RoutingDecision",
    "RoutingSink",
    "StaticPromptProvider",
    "StreamChunk",
    "StubNLIVerifier",
    "ThresholdOverflowPolicy",
    "TokenUsage",
    "ToolSpec",
    "default_config",
    "default_mode_for_role",
    "derive_family",
    "is_context_window_exceeded",
]
