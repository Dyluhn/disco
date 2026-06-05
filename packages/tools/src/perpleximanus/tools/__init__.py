"""perpleximanus.tools — the action space + secure execution (tool-sandbox-contract.md).

Fulfills the agent loop's `ToolExecutor` boundary (the last unfulfilled
dependency of the core): typed tools with single-source schemas + validation/
auto-repair, a spec-vs-instance sandbox with pluggable backends, secrets held
orchestrator-side (capabilities, never credentials, cross the boundary), and
deny-by-default egress. The loop is unaware of sandboxing.
"""

from __future__ import annotations

from .anatomy import (
    Capability,
    Tool,
    ToolContext,
    ToolDef,
    ToolExecutionError,
    ToolOutcome,
)
from .builtin import build_default_registry
from .executor import DefaultToolExecutor, validate_args
from .registry import (
    AGENT_TOOLS,
    RESEARCH_TOOLS,
    ToolRegistry,
    ToolScope,
    agent_scope,
    research_scope,
)
from .sandbox import (
    ExecResult,
    ProcessSandboxInstance,
    ProcessSandboxService,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSession,
    SandboxSpec,
)
from .secrets import (
    CapabilityBroker,
    CapabilityDenied,
    CapabilitySet,
    InMemorySecretsStore,
    SecretsStore,
)

__all__ = [
    "AGENT_TOOLS",
    "RESEARCH_TOOLS",
    "Capability",
    "CapabilityBroker",
    "CapabilityDenied",
    "CapabilitySet",
    "DefaultToolExecutor",
    "ExecResult",
    "InMemorySecretsStore",
    "ProcessSandboxInstance",
    "ProcessSandboxService",
    "SandboxError",
    "SandboxInstance",
    "SandboxService",
    "SandboxSession",
    "SandboxSpec",
    "SecretsStore",
    "Tool",
    "ToolContext",
    "ToolDef",
    "ToolExecutionError",
    "ToolOutcome",
    "ToolRegistry",
    "ToolScope",
    "agent_scope",
    "build_default_registry",
    "research_scope",
    "validate_args",
]
