"""Reusable dynamic-scope executor for phase-governed tool surfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict, Unpack

from disco.core.contract import ContractScopeGuard
from disco.core.llm import ModelExecutionPolicy

from .executor import DefaultToolExecutor
from .registry import ToolRegistry, ToolScope
from .sandbox.base import SandboxInstance
from .secrets import CapabilityBroker
from .workflow_scope import workflow_router_denial_message

_STANDARD_POLICY: ModelExecutionPolicy = ModelExecutionPolicy.standard()


class ExecutorKwargs(TypedDict, total=False):
    sandbox: SandboxInstance | None
    broker: CapabilityBroker | None
    owner_id: str
    conversation_id: str | None
    default_timeout_s: int
    model_policy: ModelExecutionPolicy
    driver_llm: tuple[str, str, str | None] | None
    read_char_budget: int | None
    scope_guard: ContractScopeGuard | None
    on_tool_success: Callable[[str], None] | None
    starter_kit: str | None
    workflow_events: Callable[[str, dict[str, Any]], Awaitable[None]] | None


class ScopedPhaseExecutor(DefaultToolExecutor):
    """A DefaultToolExecutor whose enforced scope is resolved at read time.

    The resolver owns the current allowlist names. This executor only feeds that
    scope through the normal DefaultToolExecutor rejection path, so out-of-phase
    tools still fail as ordinary ``unknown_tool`` results.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        base_scope: ToolScope,
        scope_resolver: Callable[[], ToolScope],
        **kwargs: Unpack[ExecutorKwargs],
    ) -> None:
        # DefaultToolExecutor.__init__ assigns self._scope; because _scope is a
        # property here, seed the backing value before delegating.
        self._scoped_base_scope = base_scope
        self._scoped_widened_scope = base_scope
        self._scope_resolver = scope_resolver
        self._widen_callback: Callable[[], None] | None = None
        super().__init__(registry, base_scope, **kwargs)

    @property
    def base_scope(self) -> ToolScope:
        """The constructor scope before any deferred widening delta is applied."""
        return self._scoped_base_scope

    @property
    def widened_scope(self) -> ToolScope:
        """The mutable scope used by resolvers after a phase widens."""
        return self._scoped_widened_scope

    @property  # type: ignore[override]
    def _scope(self) -> ToolScope:  # type: ignore[override]
        return self._scope_resolver()

    @_scope.setter
    def _scope(self, value: ToolScope) -> None:
        # Deferred scope deltas (MCP names, tool_search advertise/callable split)
        # arrive through the same assignment path DefaultToolExecutor uses.
        self._scoped_widened_scope = value

    def set_widen_callback(self, on_widen: Callable[[], None] | None) -> None:
        """Register a callback to apply deferred scope deltas after widening."""
        self._widen_callback = on_widen

    def _notify_widened(self) -> None:
        """Run the deferred widening callback, if one is registered."""
        if self._widen_callback is not None:
            self._widen_callback()

    def known_tool_names_for_requery(self) -> frozenset[str]:
        """Treat router-withheld registry tools as real so the executor can deny them."""

        if self._scope.preset == "workflow_router":
            return self.callable_tool_names() | self._registry.names()
        return super().known_tool_names_for_requery()

    def _unknown_tool_message(self, tool_name: str, available: list[str]) -> str:
        if self._scope.preset == "workflow_router" and tool_name in self._registry.names():
            return workflow_router_denial_message(tool_name, available)
        return super()._unknown_tool_message(tool_name, available)
