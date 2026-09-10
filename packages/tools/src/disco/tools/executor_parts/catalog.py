"""The tool catalog — the read-only projection of "what may be called".

`ToolCatalog` answers every question about the *declared* action space: which
tools a scope makes callable or advertises, where each one runs, what capability
it may use, and what shadow action profile a concrete call classifies to. It
performs no effect, owns no lifecycle, and holds no scope of its own.

The active scope is passed in on every call, never captured. That is deliberate:
`ScopedPhaseExecutor` resolves `_scope` through a property that recomputes the
AppKit/workflow phase allowlist on each read, so a catalog that cached a scope
would freeze the allowlist at construction and silently defeat strict-phase
enforcement while every existing test still passed.
"""

from __future__ import annotations

from typing import Any

from disco.core.effects import ActionProfile, EffectCapability, validate_action_profile
from disco.core.llm import ModelExecutionPolicy, ToolSpec
from pydantic import BaseModel, ValidationError

from ..anatomy import Tool
from ..registry import ToolRegistry, ToolScope
from .arguments import normalize_list_item_wrappers


class ToolCatalog:
    """Read-only projection over a registry for one caller's scope."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def tool_scope(self, tool_name: str, scope: ToolScope) -> str:
        """'sandbox' | 'in_process' | 'unknown' — where this tool executes.
        Policy input for the blast-radius gate (DC-03)."""
        for tool in self._registry.in_scope(scope):
            if tool.definition.name == tool_name:
                return tool.definition.runs_in
        return "unknown"

    def tool_scope_for_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: ToolScope,
    ) -> str:
        """Return the effective pre-execution scope for this concrete tool call.

        Some tools write through the sandbox but optionally perform host-side HTTP
        before that write. The confirmation gate calls this before execute(), so a
        tool may expose a side-effect-free ``execution_scope(args)`` hook that
        depends only on already-supplied arguments and saved config. Validation
        failures fall back to the static scope; execute() still owns the real
        invalid-arguments response.
        """
        for tool in self._registry.in_scope(scope):
            if tool.definition.name != tool_name:
                continue
            hook = getattr(tool, "execution_scope", None)
            if not callable(hook):
                return tool.definition.runs_in
            try:
                normalized = normalize_list_item_wrappers(
                    tool_name,
                    tool.definition.args_model,
                    arguments,
                )
                args = tool.definition.args_model.model_validate(normalized)
                scope_result = hook(args)
            except Exception:
                return tool.definition.runs_in
            return (
                scope_result
                if isinstance(scope_result, str)
                and scope_result in {"sandbox", "in_process", "unknown"}
                else "unknown"
            )
        return "unknown"

    def available_tools(
        self,
        scope: ToolScope,
        model_policy: ModelExecutionPolicy,
    ) -> list[ToolSpec]:
        tools = self._registry.in_scope(scope)  # registry ∩ allowed_tools
        if scope.advertised_tools is not None:
            tools = [t for t in tools if t.definition.name in scope.advertised_tools]
        # Contract #3: additionally drop any tool the model policy withholds, regardless
        # of what the scope advertises.  For the standard tier withheld_tools is empty
        # (frozenset()) so this branch is a no-op.  For the weak tier and !anchored_edit
        # this is the executor-level enforcement backstop on top of the registry's scope.
        withheld = model_policy.withheld_tools
        if withheld:
            tools = [t for t in tools if t.definition.name not in withheld]
        return [t.definition.to_spec() for t in tools]

    def callable_tool_names(self, scope: ToolScope) -> frozenset[str]:
        """Names of every tool the executor will actually run (registry ∩
        allowed_tools), IGNORING advertised_tools. The advertise/callable split
        (RP-05c) hides over-cap MCP tools from available_tools(), but they remain
        callable by qualified name — callers that need to know 'can this name be
        executed?' (e.g. the engine's unknown-tool requery gate) must use THIS,
        not available_tools(), or they will bounce withheld-but-callable tools."""
        return frozenset(t.definition.name for t in self._registry.in_scope(scope))

    def readonly_tool_names(self, scope: ToolScope) -> frozenset[str]:
        """Names of in-scope tools that only OBSERVE (ToolDef.read_only). The loop
        consults this to scope the PLANNING agent to read-only tools — a
        capability-level backstop to any name allowlist, so a misconfigured
        allowlist can't leak a write/exec tool to the planner (planner safety)."""
        return frozenset(
            t.definition.name for t in self._registry.in_scope(scope) if t.definition.read_only
        )

    def tool_names_with_capability(
        self,
        capability: EffectCapability,
        scope: ToolScope,
    ) -> frozenset[str]:
        """Names of callable tools whose declared behavior may use ``capability``.

        This is a shadow metadata query only: it does not alter advertising,
        planning, confirmation, or execution policy. Unclassified tools are
        omitted rather than guessed from their names or legacy flags.
        """

        return frozenset(
            tool.definition.name
            for tool in self._registry.in_scope(scope)
            if tool.definition.behavior is not None
            and capability in tool.definition.behavior.possible_capabilities
        )

    def unclassified_tool_names(self, scope: ToolScope) -> frozenset[str]:
        """Callable in-scope tools still awaiting K3 behavior metadata."""

        return frozenset(
            tool.definition.name
            for tool in self._registry.in_scope(scope)
            if tool.definition.behavior is None
        )

    def assert_behavior_complete(self, scope: ToolScope) -> None:
        """Fail a registry conformance gate if any callable tool is unclassified."""

        missing = sorted(self.unclassified_tool_names(scope))
        if missing:
            raise ValueError(f"callable tools lack behavior metadata: {', '.join(missing)}")

    def action_profile_for_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        scope: ToolScope,
    ) -> ActionProfile | None:
        """Return the validated shadow action profile for one concrete call.

        ``None`` is conservative and means unknown, out-of-scope, invalid
        arguments, unclassified behavior, or an invalid classifier result. The
        normal execute path still reports its existing detailed argument error.
        """

        tool = self._registry.get(tool_name, scope=scope)
        if tool is None:
            return None
        normalized = normalize_list_item_wrappers(
            tool_name,
            tool.definition.args_model,
            arguments,
        )
        try:
            args = tool.definition.args_model.model_validate(normalized)
        except (TypeError, ValueError, ValidationError):
            return None
        profile, _reason = self.profile_for_validated_call(tool, args)
        return profile

    @staticmethod
    def profile_for_validated_call(
        tool: Tool,
        args: BaseModel,
    ) -> tuple[ActionProfile | None, str | None]:
        behavior = tool.definition.behavior
        if behavior is None:
            return None, "tool behavior is unclassified"

        classifier = getattr(tool, "action_profile", None)
        if classifier is None:
            return behavior.static_profile(), None
        if not callable(classifier):
            return None, "tool action_profile attribute is not callable"
        try:
            profile = classifier(args)
            if not isinstance(profile, ActionProfile):
                return None, "tool action classifier returned a non-ActionProfile value"
            return validate_action_profile(behavior, profile), None
        except Exception as exc:  # noqa: BLE001 — shadow metadata must not alter execution
            return None, f"tool action classifier was invalid: {exc}"
