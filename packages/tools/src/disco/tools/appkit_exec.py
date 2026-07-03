"""AppKit EPIC F — the phase-aware tool executor.

`AppKitToolExecutor` is a thin `DefaultToolExecutor` subclass whose security scope
is DYNAMIC: it is recomputed every time it is read from (loop_mode, AppKit phase,
autonomous) via :func:`appkit_effective_scope`. Because every reader on the
executor — `available_tools()`, `callable_tool_names()`, `readonly_tool_names()`,
`tool_scope()`, and `execute()`'s registry resolution — keys off `self._scope`,
making `_scope` a phase-aware property enforces the strict AppKit allowlist
uniformly, on the ALLOWLIST (callable set), not merely the advertised/visible set.

Phase transitions happen in `execute()` (a wrapper around the base executor, NOT
an edit to the app tools or the engine):

* a SUCCESSFUL ``app_create`` advances ``planning → build`` (the app scaffold +
  the two ``.disco`` specs now exist, so the rest of the mutators unlock);
* a SUCCESSFUL ``request_custom_build`` (which only reaches `execute()` AFTER the
  BlastRadiusConfirm human gate) advances to ``custom_build`` and WIDENS scope to
  the normal ``agent_scope`` — at which point the deferred MCP delta (P0) is
  applied so MCP names become callable for the first time.
"""

from __future__ import annotations

from collections.abc import Callable

from disco.core import ToolCall, ToolResult
from disco.core.llm import OperatingMode

from .appkit_scope import (
    REQUEST_CUSTOM_BUILD,
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
)
from .executor import DefaultToolExecutor
from .registry import ToolRegistry, ToolScope


class AppKitToolExecutor(DefaultToolExecutor):
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
        **kwargs: object,
    ) -> None:
        # These MUST exist before super().__init__ runs, because the base __init__
        # does `self._scope = scope`, which routes through our property setter below.
        self._appkit_phase = appkit_phase
        self._appkit_base_scope = base_scope
        self._appkit_autonomous = autonomous
        self._appkit_mode_getter = mode_getter
        self._appkit_on_widen = on_widen
        # Holds the widened (custom_build) scope; the base __init__'s assignment and
        # any later MCP-delta model_copy land here via the setter.
        self._appkit_widened: ToolScope = base_scope
        super().__init__(registry, scope, **kwargs)  # type: ignore[arg-type]

    # ---- the dynamic, phase-aware scope -------------------------------------

    @property  # type: ignore[override]
    def _scope(self) -> ToolScope:  # type: ignore[override]
        if self._appkit_phase.phase == AppKitPhase.CUSTOM_BUILD:
            # Widened: the base agent_scope (+ any applied MCP delta), held in _widened.
            return self._appkit_widened
        loop_mode = self._appkit_mode_getter() if self._appkit_mode_getter else None
        return appkit_effective_scope(
            loop_mode=loop_mode,
            phase=self._appkit_phase.phase,
            base_scope=self._appkit_base_scope,
            autonomous=self._appkit_autonomous,
        )

    @_scope.setter
    def _scope(self, value: ToolScope) -> None:
        # The custom_build / MCP path mutates scope via `self._scope = ...model_copy()`;
        # store it as the widened scope (only consulted once phase == CUSTOM_BUILD).
        self._appkit_widened = value

    def set_widen_callback(self, on_widen: Callable[[], None] | None) -> None:
        """Wire the deferred MCP-delta application (P0). Called by the runtime after
        the MCP tool snapshot is known — applied only when scope widens to custom_build."""
        self._appkit_on_widen = on_widen

    # ---- phase transitions (wrapper around the base executor) ----------------

    async def execute(self, call: ToolCall) -> ToolResult:
        result = await super().execute(call)
        if not result.success:
            return result
        name = call.tool_name
        phase = self._appkit_phase.phase
        if name == "app_create" and phase == AppKitPhase.PLANNING:
            # The scaffold + the two .disco specs now exist → unlock the build mutators.
            self._appkit_phase.phase = AppKitPhase.BUILD
        elif name == REQUEST_CUSTOM_BUILD and phase in (
            AppKitPhase.PLANNING,
            AppKitPhase.BUILD,
        ):
            # Reached here ONLY post-confirm (HIGH-risk → BlastRadiusConfirm gate).
            # Widen to the normal agent_scope, then apply the deferred MCP delta so
            # MCP names become callable for the first time (P0: no earlier bypass).
            self._appkit_phase.phase = AppKitPhase.CUSTOM_BUILD
            self._appkit_widened = self._appkit_base_scope
            if self._appkit_on_widen is not None:
                self._appkit_on_widen()
        return result
