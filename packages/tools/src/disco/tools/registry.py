"""Tool registry & scoping — tool-sandbox-contract.md §8.

The available toolset is a registry; surfaces (Research vs Agent) expose
different *scopes* of it (BoD §8). `available_tools()` returns only in-scope
tools, so the model is never shown — and the executor never runs (§4) — a tool
outside the active surface's scope. Scope is the first-line capability boundary;
the sandbox/egress/secrets rules are the hard backstops.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from disco.core.llm import Requirement

from .anatomy import Tool


class ToolScope(BaseModel):
    """[CONTRACT] The set of tools a SURFACE exposes (BoD §8).

    `allowed_tools` is the SECURITY allowlist — the executor only calls tools
    in this set. `advertised_tools` (RP-05c) is the VISIBILITY set — what
    available_tools() returns to the LLM. None means "advertise all allowed"
    (today's behavior). A tool that is allowed but not advertised is still
    callable by qualified name; the planner-safety readonly_tool_names backstop
    always keys off allowed_tools, never the advertised subset.
    """

    model_config = ConfigDict(frozen=True)
    allowed_tools: frozenset[str]
    advertised_tools: frozenset[str] | None = None  # None = advertise all allowed
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
        # line-number-targeted edits — the reliable way to edit LARGE files (any model)
        "file_replace_lines",
        "file_insert_lines",
        # W4 — anchored str-replace for capable models (ANCHORED_EDIT). Callable by
        # all agents but WITHHELD from advertised_tools for the weak tier; see
        # agent_scope() below. Capable models get it via model_caps=ANCHORED_EDIT.
        "file_str_replace",
        "file_list",
        "shell",
        "shell_exec",
        "shell_view",
        "shell_wait",
        "shell_write_to_process",
        "shell_kill_process",
        "code_exec",
        "browser",
        "deploy_preview",
        "server_status",
        # plan-mode meta tools: propose a plan (planning) + report capstones (execution).
        "submit_plan",
        "plan_step",
        "think",  # NO-OP reasoning scratchpad — let a small model "say" things without side effects
        "sheet_generate",
        "slides_generate",
        "audio_overview",
        "image_generate",
        # C-EDIT-4: deck_patch — targeted RFC-6902 edits to authored deck JSON
        "deck_patch",
        # C20 — `delegate_explore`: EXECUTION-only. It is NOT a pure read — the
        # call DISPATCHES a subagent (an action that yields an observation), so
        # it does NOT belong in the PLANNING surface. The engine appends it via
        # its virtual-singleton path in execution mode (and never in the
        # planning branch), so the planner never sees it; the capability
        # backstop also excludes it (`read_only=False` on the tool def).
        "delegate_explore",
    }
)


# W4: tools withheld from the advertised set for weak-tier (non-ANCHORED_EDIT) models.
# These remain in AGENT_TOOLS (callable by qualified name) but are hidden from
# available_tools() so the model's context never sees a tool it can't use well.
_ANCHORED_EDIT_TOOLS: frozenset[str] = frozenset({"file_str_replace"})
_WEAK_TIER_ADVERTISED: frozenset[str] = AGENT_TOOLS - _ANCHORED_EDIT_TOOLS

# C6: artifact_mode tool scope — a STRICT SUBSET of AGENT_TOOLS with NO shell/browser/
# plan-gate/code_exec/file_str_replace/delegate_explore. Includes line-edit tools
# (file_replace_lines / file_insert_lines) so artifacts remain editable post-creation.
ARTIFACT_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "file_write",
        "file_append",
        "file_edit",
        # line-number-targeted edits — present so artifacts are editable without shell
        "file_replace_lines",
        "file_insert_lines",
        "file_list",
        "search",
        "extract",
        "sheet_generate",
        "slides_generate",
        "image_generate",
        "audio_overview",
        "think",
        # C-EDIT-4: deck editing is safe in artifact scope (no shell/browser)
        "deck_patch",
    }
)


def artifact_scope() -> ToolScope:
    """Return the ToolScope for artifact mode (C6) — NO shell/browser/plan-gate.

    ARTIFACT_TOOLS is a strict subset of AGENT_TOOLS: file writers + asset
    generators + search/extract + think. Excludes shell*, browser, deploy_preview,
    server_status, submit_plan, plan_step, code_exec, file_str_replace,
    delegate_explore. The INTERACTIVE/NeverConfirm loop is low-risk by design;
    the boundary is the intersection: artifact mode must NOT silently grant
    shell or browser access."""
    return ToolScope(allowed_tools=ARTIFACT_TOOLS, preset="artifact")


def research_scope() -> ToolScope:
    return ToolScope(allowed_tools=RESEARCH_TOOLS, preset="research")


def agent_scope(
    *, model_caps: frozenset[Requirement] = frozenset()
) -> ToolScope:
    """Return the ToolScope for an agent surface.

    W4 one-policy-point: `file_str_replace` is in `allowed_tools` for all agents
    (callable by qualified name) but is only ADVERTISED to models that benchmark
    well on anchored edits (`Requirement.ANCHORED_EDIT`). The weak tier (default)
    sees only the whole-file + line-number tools and the W3 syntax gate catches
    any bad writes. Pass `model_caps` from the model's `ModelEntry.capabilities`
    to opt a capable model in.
    """
    if Requirement.ANCHORED_EDIT in model_caps:
        # Capable tier: all tools advertised (advertised_tools=None = show all allowed).
        return ToolScope(allowed_tools=AGENT_TOOLS, preset="agent")
    # Weak/default tier: withhold file_str_replace from advertised set.
    return ToolScope(
        allowed_tools=AGENT_TOOLS,
        advertised_tools=_WEAK_TIER_ADVERTISED,
        preset="agent",
    )
