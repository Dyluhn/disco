"""Tool registry & scoping — tool-sandbox-contract.md §8.

The available toolset is a registry; surfaces (Research vs Agent) expose
different *scopes* of it (BoD §8). `available_tools()` returns only in-scope
tools, so the model is never shown — and the executor never runs (§4) — a tool
outside the active surface's scope. Scope is the first-line capability boundary;
the sandbox/egress/secrets rules are the hard backstops.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .anatomy import Tool


class ToolScope(BaseModel):
    """[CONTRACT] The set of tools a SURFACE exposes (BoD §8)."""

    model_config = ConfigDict(frozen=True)
    allowed_tools: frozenset[str]
    preset: str | None = None  # "research" | "agent" | custom


class ToolRegistry:
    """[CONTRACT] The catalogue of all tools. The executor asks it for in-scope
    tools and to resolve a name within a scope. `get()` returns None if the name
    is absent OR out-of-scope — the executor turns that into `unknown_tool`."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.name] = tool

    def get(self, name: str, *, scope: ToolScope) -> Tool | None:
        if name not in scope.allowed_tools:
            return None  # out of scope is indistinguishable from absent (§4)
        return self._tools.get(name)

    def in_scope(self, scope: ToolScope) -> list[Tool]:
        return [t for n, t in self._tools.items() if n in scope.allowed_tools]

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)


# Surface presets (BoD §8). Research excludes world-affecting tools; Agent gets
# the full toolset. These name the tools; the actual registered set may be a
# subset in v1 (browser/deploy deferred) — scoping intersects with what exists.
RESEARCH_TOOLS = frozenset({"search", "extract", "file_read", "file_list", "code_exec"})
AGENT_TOOLS = frozenset(
    {
        "search",
        "extract",
        "file_read",
        "file_write",
        "file_append",
        "file_edit",
        "file_list",
        "shell",
        "code_exec",
        "browser",
        "deploy_preview",
        # preview lifecycle (§E): read-only health + bounded restart + detached serve.
        "preview_status",
        "restart_preview",
        "run_server",
        # plan-mode meta tools: propose a plan (planning) + report capstones (execution).
        "submit_plan",
        "plan_step",
    }
)


def research_scope() -> ToolScope:
    return ToolScope(allowed_tools=RESEARCH_TOOLS, preset="research")


def agent_scope() -> ToolScope:
    return ToolScope(allowed_tools=AGENT_TOOLS, preset="agent")
