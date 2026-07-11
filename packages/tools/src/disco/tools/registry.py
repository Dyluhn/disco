"""Tool registry & scoping — tool-sandbox-contract.md §8.

The available toolset is a registry; surfaces (Research vs Agent) expose
different *scopes* of it (BoD §8). `available_tools()` returns only in-scope
tools, so the model is never shown — and the executor never runs (§4) — a tool
outside the active surface's scope. Scope is the first-line capability boundary;
the sandbox/egress/secrets rules are the hard backstops.
"""

from __future__ import annotations

from disco.core.llm import ModelExecutionPolicy
from pydantic import BaseModel, ConfigDict

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
    # Workflow RUN scopes carry the human-approved output contract path so
    # executor-level refusals can name the designed exit without importing loop
    # state into the tools layer.
    workflow_output_path_template: str | None = None


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
        # CD-TOOLS-2 — atomic exact-match batch replace; anchored-edit tier (withheld from the
        # weak advertised set via ModelExecutionPolicy.withheld_tools, callable by all).
        "exact_replace",
        # CD-TOOLS-7 — buffered transactional batch of deterministic file transforms.
        "run_project_script",
        "file_list",
        "shell",
        "shell_exec",
        "shell_view",
        "shell_wait",
        "shell_write_to_process",
        "shell_kill_process",
        "code_exec",
        "browser",
        # W-45 — verify_web_app: structured web-app self-test → the finish gate's
        # clean pass/fail verdict (kills the 25-40× browser reload verify-loop).
        "verify_web_app",
        # EPIC D3 — design_lint: read-only design-slop scanner (designspec-
        # justification aware). A verification PROBE like verify_web_app, not a
        # productive edit (registered beside it in the non-productive probe set).
        "design_lint",
        # EPIC F — platform-owned preview surface. These four SUPERSEDE the old
        # `deploy_preview` placeholder (which was a deferred, never-registered name):
        # the model declares intent (dir/framework/command) and the platform owns
        # the port/serving/health. They are REGISTERED (build_default_registry), so
        # they must be in scope here or the agent resolves them as unknown_tool.
        "preview_start",
        "preview_status",
        "preview_logs",
        "preview_stop",
        "server_status",
        # plan-mode meta tools: propose a plan (planning) + report capstones (execution).
        "submit_plan",
        # runthru-v2 (#3): declarative full-state progress snapshot. The legacy
        # plan_step tool stays registered for replay/back-compat but is not in
        # the agent scope.
        "update_plan_progress",
        "think",  # NO-OP reasoning scratchpad — let a small model "say" things without side effects
        # CXT-2: durable .disco/context/* working memory (read any kind; write narrative kinds).
        "context_memory",
        # Legacy AppKit wrappers still used by governed persisted-AppSpec paths.
        # The overlapping create/edit/design tools are v2-only in strict AppKit mode.
        "app_set_tweak",
        "app_snapshot_version",
        # P7: materialize the contract's host-owned starter frame
        "scaffold_starter",
        # WO-TC2: trusted components — verified vendored security cores (free-form
        # Build ONLY; strict AppKit / artifact / research scopes exclude the names).
        "add_trusted_component",
        "eject_trusted_component",
        # document/report: model authors parts; host assembles/stamps the export.
        "doc_set_section",
        "doc_export",
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


# W4: backward-compat constants kept for external imports (e.g. agent-server tests).
# The new agent_scope() uses model_policy.withheld_tools instead of these directly.
# Do NOT use these constants in new code — derive from ModelExecutionPolicy.withheld_tools.
_ANCHORED_EDIT_TOOLS: frozenset[str] = frozenset({"exact_replace"})
_WEAK_TIER_ADVERTISED: frozenset[str] = AGENT_TOOLS - _ANCHORED_EDIT_TOOLS

# C6: artifact_mode tool scope — a STRICT SUBSET of AGENT_TOOLS with NO shell/browser/
# preview/plan-gate/code_exec/legacy anchored-edit/delegate_explore. Includes line-edit tools
# (file_replace_lines / file_insert_lines) so artifacts remain editable post-creation.
ARTIFACT_TOOLS: frozenset[str] = frozenset(
    {
        "file_read",
        "file_write",
        "run_project_script",  # CD-TOOLS-7 — transactional batch of file transforms
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
        # CXT-2: durable context memory is safe in artifact scope (workspace FS only)
        "context_memory",
        # Legacy AppKit wrappers still used by governed persisted-AppSpec paths.
        "app_set_tweak",
        "app_snapshot_version",
        # P7: materialize the contract's host-owned starter frame (workspace FS only)
        "scaffold_starter",
        # document/report: workspace-only part authoring/export.
        "doc_set_section",
        "doc_export",
        # C-EDIT-4: deck editing is safe in artifact scope (no shell/browser)
        "deck_patch",
    }
)


def artifact_scope() -> ToolScope:
    """Return the ToolScope for artifact mode (C6) — NO shell/browser/plan-gate.

    ARTIFACT_TOOLS is a strict subset of AGENT_TOOLS: file writers + asset
    generators + search/extract + think. Excludes shell*, browser, preview_*,
    server_status, submit_plan, plan_step, code_exec, legacy anchored-edit,
    delegate_explore. The INTERACTIVE/NeverConfirm loop is low-risk by design;
    the boundary is the intersection: artifact mode must NOT silently grant
    shell or browser access."""
    return ToolScope(allowed_tools=ARTIFACT_TOOLS, preset="artifact")


def research_scope() -> ToolScope:
    return ToolScope(allowed_tools=RESEARCH_TOOLS, preset="research")


def agent_scope(*, model_policy: ModelExecutionPolicy) -> ToolScope:
    """Return the ToolScope for an agent surface, narrowed by model_policy.

    Contract #4: ONE exact keyword signature (not "policy or withheld_tools").
    AGENT_TOOLS is the security allowlist (what is *callable*); the advertised
    set shown to the LLM is narrowed by model_policy.withheld_tools.  Withheld
    tools remain in allowed_tools so the engine repair loop and the planner-
    safety backstop (readonly_tool_names) continue to key off the full
    allowed set — they must never see a narrower boundary than the security one.

    Tier / capability behaviour (driven entirely by the policy object):
      • standard + anchored_edit=True  → all scoped tools advertised
      • standard + anchored_edit=False → exact_replace withheld
      • weak    + anchored_edit=True   → update_plan_progress withheld
      • weak    + anchored_edit=False  → exact_replace and update_plan_progress withheld
    """
    withheld = model_policy.withheld_tools
    if not withheld:
        # Standard no-op: show the complete toolset (advertised_tools=None = all allowed).
        return ToolScope(allowed_tools=AGENT_TOOLS, preset="agent")
    return ToolScope(
        allowed_tools=AGENT_TOOLS,
        advertised_tools=AGENT_TOOLS - withheld,
        preset="agent",
    )
