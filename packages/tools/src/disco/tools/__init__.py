"""disco.tools — the action space + secure execution (tool-sandbox-contract.md).

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
from .appkit_exec import AppKitToolExecutor
from .appkit_scope import (
    APPKIT_MUTATORS,
    APPKIT_PROBES,
    APPKIT_READ_TOOLS,
    REQUEST_CUSTOM_BUILD,
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
)
from .builtin import build_default_registry
from .executor import DefaultToolExecutor, validate_args
from .registry import (
    AGENT_TOOLS,
    ARTIFACT_TOOLS,
    RESEARCH_TOOLS,
    ToolRegistry,
    ToolScope,
    agent_scope,
    artifact_scope,
    research_scope,
)
from .sandbox import (
    REGISTRY_EGRESS_ALLOW,
    ExecResult,
    ProcessSandboxInstance,
    ProcessSandboxService,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSession,
    SandboxSpec,
)
from .scoped_exec import ScopedPhaseExecutor
from .secrets import (
    CapabilityBroker,
    CapabilityDenied,
    CapabilitySet,
    InMemorySecretsStore,
    SecretsStore,
)
from .workflow_scope import (
    WORKFLOW_ROUTER_ALLOWED_TOOLS,
    WORKFLOW_ROUTER_CONTEXT_TOOLS,
    WORKFLOW_ROUTER_TOOLS,
    WorkflowPhase,
    WorkflowPhaseState,
    workflow_effective_scope,
)

__all__ = [
    "AGENT_TOOLS",
    "APPKIT_MUTATORS",
    "APPKIT_PROBES",
    "APPKIT_READ_TOOLS",
    "ARTIFACT_TOOLS",
    "AppKitPhase",
    "AppKitPhaseState",
    "AppKitToolExecutor",
    "REQUEST_CUSTOM_BUILD",
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
    "REGISTRY_EGRESS_ALLOW",
    "SandboxError",
    "SandboxInstance",
    "SandboxService",
    "SandboxSession",
    "SandboxSpec",
    "SecretsStore",
    "ScopedPhaseExecutor",
    "Tool",
    "ToolContext",
    "ToolDef",
    "ToolExecutionError",
    "ToolOutcome",
    "ToolRegistry",
    "ToolScope",
    "WORKFLOW_ROUTER_ALLOWED_TOOLS",
    "WORKFLOW_ROUTER_CONTEXT_TOOLS",
    "WORKFLOW_ROUTER_TOOLS",
    "WorkflowPhase",
    "WorkflowPhaseState",
    "agent_scope",
    "appkit_effective_scope",
    "artifact_scope",
    "build_default_registry",
    "research_scope",
    "validate_args",
    "workflow_effective_scope",
]
