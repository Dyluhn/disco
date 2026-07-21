"""AppKit EPIC F — the phase-aware tool executor.

`AppKitToolExecutor` is a thin `DefaultToolExecutor` subclass whose security scope
is DYNAMIC: it is recomputed every time it is read from (loop_mode, AppKit phase,
autonomous) via :func:`appkit_effective_scope`. Because every reader on the
executor — `available_tools()`, `callable_tool_names()`, `readonly_tool_names()`,
`tool_scope()`, and `execute()`'s registry resolution — keys off `self._scope`,
making `_scope` a phase-aware property enforces the strict AppKit allowlist
uniformly, on the ALLOWLIST (callable set), not merely the advertised/visible set.

Phase reconciliation happens through the generic executor preparation seam:

* current validated ``AppSpec`` + ``DesignSpec`` bytes select ``build`` (and
  their absence/corruption selects ``planning``), so rollback can downgrade;
* an exact, generation-consistent persisted ``request_custom_build`` action and
  successful observation selects sticky ``custom_build`` and WIDENS scope to the
  normal ``agent_scope`` — at which point the deferred MCP delta (P0) is applied
  once. Tool success alone never authorizes widening across a crash window.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Unpack

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    Event,
    ObservationEvent,
    ToolCall,
    ToolResult,
    WorkspaceMutationEvent,
    agent_view_consistent_events,
)
from disco.core.appkit import (
    APPSPEC_RELPATH,
    DESIGNSPEC_RELPATH,
    load_app_spec_from_bytes,
    load_design_spec_from_bytes,
)
from disco.core.llm import OperatingMode

from .appkit_scope import (
    APPKIT_MUTATORS,
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
    fold_appkit_phase_evidence,
)
from .registry import ToolRegistry, ToolScope
from .scoped_exec import ExecutorKwargs, ScopedPhaseExecutor


class AppKitToolExecutor(ScopedPhaseExecutor):
    """A DefaultToolExecutor whose scope is the phase-aware AppKit allowlist."""

    def __init__(
        self,
        registry: ToolRegistry,
        scope: ToolScope,
        *,
        appkit_phase: AppKitPhaseState,
        base_scope: ToolScope,
        autonomous: bool,
        mode_getter: Callable[[], OperatingMode | None] | None = None,
        on_widen: Callable[[], None] | None = None,
        **kwargs: Unpack[ExecutorKwargs],
    ) -> None:
        self._appkit_phase = appkit_phase
        # Public marker the core finish gate duck-types (getattr) to detect
        # strict AppKit mode even while the phase allowlist hides the verify
        # probe (pre-app_create) — a finish in that window must still be gated.
        self.appkit_phase = appkit_phase
        self._appkit_base_scope = base_scope
        self._appkit_autonomous = autonomous
        self._appkit_mode_getter = mode_getter
        self._appkit_on_widen = on_widen
        self._appkit_base_primitive: str | None = None
        self._appkit_workspace_generation: tuple[int, int] | None = None
        self._appkit_custom_authorized = False
        self._appkit_scope_widened = False
        self._appkit_widen_applied = False

        def _scope_resolver() -> ToolScope:
            loop_mode = self._appkit_mode_getter() if self._appkit_mode_getter else None
            return appkit_effective_scope(
                loop_mode=loop_mode,
                phase=self._appkit_phase.phase,
                base_scope=self.widened_scope,
                autonomous=self._appkit_autonomous,
            )

        super().__init__(
            registry,
            scope,
            scope_resolver=_scope_resolver,
            **kwargs,
        )
        self.set_widen_callback(on_widen)

    def set_widen_callback(self, on_widen: Callable[[], None] | None) -> None:
        """Wire the deferred MCP-delta application (P0). Called by the runtime after
        the MCP tool snapshot is known — applied only when scope widens to custom_build."""
        self._appkit_on_widen = on_widen
        super().set_widen_callback(on_widen)
        if self._appkit_custom_authorized:
            self._activate_custom_widening()

    def _activate_custom_widening(self) -> None:
        """Apply the durable custom-build widening at most once per executor."""

        self._appkit_phase.phase = AppKitPhase.CUSTOM_BUILD
        if not self._appkit_scope_widened:
            self._scope = self._appkit_base_scope
            self._appkit_scope_widened = True
        if self._appkit_widen_applied or self._appkit_on_widen is None:
            return
        self._notify_widened()
        self._appkit_widen_applied = True

    def _relevant_workspace_generation(self, events: Iterable[Event]) -> tuple[int, int]:
        """Generation key for AppKit spec validation, ignoring pure chat/status churn."""

        projected = agent_view_consistent_events(events)
        mutating_actions = {
            event.id: event
            for event in projected
            if isinstance(event, ActionEvent) and event.tool_call.tool_name in APPKIT_MUTATORS
        }
        relevant_seq = -1
        for event in projected:
            if isinstance(event, WorkspaceMutationEvent) or (
                isinstance(event, ActionEvent) and event.id in mutating_actions
            ):
                relevant_seq = max(relevant_seq, event.seq or -1)
            elif isinstance(event, (ObservationEvent, AgentErrorEvent)):
                if event.action_id in mutating_actions:
                    relevant_seq = max(relevant_seq, event.seq or -1)
        sandbox_generation = int(getattr(self._sandbox, "generation", 0) or 0)
        return sandbox_generation, relevant_seq

    async def prepare_for_events(self, events: list[Event]) -> None:
        """Recover dynamic AppKit authority before advertising or executing tools.

        Event evidence may only make CUSTOM_BUILD sticky. Otherwise the current
        validated AppSpec+DesignSpec pair decides BUILD vs PLANNING, allowing a
        rollback to downgrade scope. A transient workspace read failure preserves
        an already-proven BUILD executor but never upgrades a fresh one.
        """

        if fold_appkit_phase_evidence(events) is AppKitPhase.CUSTOM_BUILD:
            self._appkit_custom_authorized = True
            self._activate_custom_widening()
            return

        generation = self._relevant_workspace_generation(events)
        if generation == self._appkit_workspace_generation:
            return
        sandbox = self._sandbox
        if sandbox is None:
            if self._appkit_phase.phase is not AppKitPhase.BUILD:
                self._appkit_phase.phase = AppKitPhase.PLANNING
                self._appkit_base_primitive = None
            return
        try:
            app_bytes = await sandbox.read_file(APPSPEC_RELPATH)
            design_bytes = await sandbox.read_file(DESIGNSPEC_RELPATH)
        except FileNotFoundError:
            self._appkit_phase.phase = AppKitPhase.PLANNING
            self._appkit_base_primitive = None
            self._appkit_workspace_generation = generation
            return
        except Exception:  # noqa: BLE001 - transient sandbox/read infrastructure
            if self._appkit_phase.phase is not AppKitPhase.BUILD:
                self._appkit_phase.phase = AppKitPhase.PLANNING
                self._appkit_base_primitive = None
            return
        try:
            app = load_app_spec_from_bytes(app_bytes)
            load_design_spec_from_bytes(design_bytes)
        except (TypeError, ValueError):
            self._appkit_phase.phase = AppKitPhase.PLANNING
            self._appkit_base_primitive = None
            self._appkit_workspace_generation = generation
            return
        self._appkit_base_primitive = app.app_kind
        self._appkit_phase.phase = AppKitPhase.BUILD
        self._appkit_workspace_generation = generation

    def known_tool_names_for_requery(self) -> frozenset[str]:
        """Distinguish truly unknown names from strict-scope denials.

        Rung-7 may silently requery a name that does not exist.  A registered
        tool withheld by strict AppKit is different: it must reach ``execute``
        once so the model receives the canonical out-of-scope observation and
        the current actionable allowlist.  This widens only the driver's
        recognition set, never AppKit callability or execution authority.
        """

        return self.callable_tool_names() | self._registry.names()

    def _unknown_tool_message(self, tool_name: str, available: list[str]) -> str:
        """Give registered strict-scope denials an actionable, non-retry contract."""

        if tool_name in self._registry.names() and tool_name not in self.callable_tool_names():
            recovery = self._strict_scope_recovery(available)
            return (
                f"strict AppKit scope denied registered tool {tool_name!r}; it was not "
                f"executed. Available now: {available}. Raw file/shell/code/starter/preview "
                "tools remain disabled while strict AppKit scope is active; do not retry "
                f"this call. Recovery: {recovery}"
            )
        return super()._unknown_tool_message(tool_name, available)

    def _strict_scope_recovery(self, available: list[str]) -> str:
        """Direct recovery using only productive tools callable in this phase."""

        callable_now = set(available)
        if "submit_plan" in callable_now:
            return "inspect with offered read tools if needed, then call `submit_plan`."
        if "verify_appkit_app" in callable_now:
            preferred = (
                "app_update_content",
                "app_add_section",
                "app_set_design",
                "app_add_primitive",
                "design_lint",
                "verify_appkit_app",
                "app_snapshot_version",
            )
            offered = [
                name
                for name in preferred
                if name in callable_now
                and not (
                    name == "app_add_primitive" and self._appkit_base_primitive == "local_list"
                )
            ]
            rendered = ", ".join(f"`{name}`" for name in offered)
            return f"continue with a currently offered semantic operation: {rendered}."
        if "app_create" in callable_now:
            return "call `app_create` to establish the semantic AppKit scaffold."
        return "choose a tool from the Available now list."

    @property
    def _appkit_widened(self) -> ToolScope:
        """Backward-compatible alias for the widened AppKit scope storage."""
        return self.widened_scope

    @_appkit_widened.setter
    def _appkit_widened(self, value: ToolScope) -> None:
        self._scope = value

    # ---- immediate post-call cache (durable reconciliation owns authority) ----

    async def execute(self, call: ToolCall) -> ToolResult:
        result = await super().execute(call)
        if not result.success:
            return result
        name = call.tool_name
        phase = self._appkit_phase.phase
        if name == "app_create" and phase == AppKitPhase.PLANNING:
            # The scaffold + the two .disco specs now exist → unlock the build mutators.
            structured = result.structured or {}
            base_primitive = structured.get("primitive_id") or structured.get("app_kind")
            if isinstance(base_primitive, str):
                self._appkit_base_primitive = base_primitive
            self._appkit_phase.phase = AppKitPhase.BUILD
        return result
