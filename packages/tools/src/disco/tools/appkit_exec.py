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
from typing import Unpack

from disco.core import ToolCall, ToolResult
from disco.core.llm import OperatingMode

from .appkit_scope import (
    REQUEST_CUSTOM_BUILD,
    AppKitPhase,
    AppKitPhaseState,
    appkit_effective_scope,
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

    def known_tool_names_for_requery(self) -> frozenset[str]:
        """Distinguish truly unknown names from strict-scope denials.

        Rung-7 may silently requery a name that does not exist.  A registered
        tool withheld by strict AppKit is different: it must reach ``execute``
        once so the model receives the canonical out-of-scope observation and
        the current actionable allowlist.  This widens only the driver's
        recognition set, never AppKit callability or execution authority.
        """

        return self.callable_tool_names() | self._registry.names()

    @property
    def _appkit_widened(self) -> ToolScope:
        """Backward-compatible alias for the widened AppKit scope storage."""
        return self.widened_scope

    @_appkit_widened.setter
    def _appkit_widened(self, value: ToolScope) -> None:
        self._scope = value

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
            self._scope = self._appkit_base_scope
            self._notify_widened()
        return result
