"""Deterministic model-visible and execute-allowed tool calculation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from ..llm import OperatingMode
from .fc_kit import _nearest_tool_name
from .messages import _PLAN_EXPLORE_READ_CAP
from .tool_specs import (
    _ask_user_tool_singleton,
    _clarify_tool_singleton,
    _notify_user_tool_singleton,
    _propose_plan_update_tool_singleton,
    _questions_v2_tool_singleton,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .ports import (
        ConversationModePort,
        FinishVerificationPort,
        GateCounterPort,
        PlanLifecyclePort,
        ToolExecutionPort,
    )


    class _LoopFacet(


        ConversationModePort,


        FinishVerificationPort,


        GateCounterPort,


        PlanLifecyclePort,


        ToolExecutionPort,


        Protocol,


    ):

        """The loop capability this module uses: conversation mode, finish verification, gate

        counters, the plan lifecycle, tool execution.

        """

_FORCE_SUBMIT_READ_GRACE = 3


class ToolVisibility:
    """Own the advertised/accepted tool surface for one loop."""

    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop

    def readonly_tool_names(self) -> frozenset[str] | None:
        provider = getattr(self._loop.executor, "readonly_tool_names", None)
        if provider is None:
            return None
        try:
            return frozenset(provider())
        except Exception:  # noqa: BLE001
            return None

    def _available_tools(self, available_tools: list | None) -> list:
        return (
            available_tools
            if available_tools is not None
            else self._loop.executor.available_tools()
        )

    @staticmethod
    def _planner_name_allowed(
        name: str | None,
        readonly: frozenset[str] | None,
        allow: frozenset[str],
        *,
        anonymous_when_unknown: bool = False,
    ) -> bool:
        if name is None:
            return anonymous_when_unknown and readonly is None and not allow
        if readonly is not None and name not in readonly:
            return False
        return not allow or name in allow

    def planning_allowed_tool_names(
        self,
        available_tools: list | None = None,
    ) -> frozenset[str]:
        readonly = self.readonly_tool_names()
        allow = self._loop._planning_tools
        names = {
            name
            for tool in self._available_tools(available_tools)
            if isinstance((name := getattr(tool, "name", None)), str)
            and self._planner_name_allowed(name, readonly, allow)
        }
        names.add(self._loop._plan_tool)
        names.update({"ask_user", "questions_v2", "clarify"})
        return frozenset(names)

    def force_submit_read_calls_remaining(self) -> int:
        return max(
            0,
            (_PLAN_EXPLORE_READ_CAP + _FORCE_SUBMIT_READ_GRACE) - self._loop._plan_explore_reads,
        )

    def _forced_submit_tools(
        self,
        tools: list,
        force_read_tools: frozenset[str] | None,
    ) -> list | None:
        if self._loop._plan_tool is None:
            return None
        plan_name = getattr(
            self._loop._plan_tool,
            "name",
            self._loop._plan_tool,
        )
        readonly = self.readonly_tool_names() or frozenset()
        if self._loop._planning_tools:
            readonly &= self._loop._planning_tools
        if force_read_tools is not None:
            readonly &= force_read_tools
        reads_allowed = self.force_submit_read_calls_remaining() > 0
        narrowed = [
            tool
            for tool in tools
            if getattr(tool, "name", None) == plan_name
            or (
                reads_allowed
                and getattr(tool, "name", None) is not None
                and getattr(tool, "name", None) in readonly
            )
        ]
        return narrowed or None

    def _planning_tools(
        self,
        tools: list,
        *,
        force_submit_only: bool,
        force_read_tools: frozenset[str] | None,
    ) -> list:
        if force_submit_only:
            forced = self._forced_submit_tools(tools, force_read_tools)
            if forced is not None:
                return forced
        readonly = self.readonly_tool_names()
        allow = self._loop._planning_tools
        planner_tools = [
            tool
            for tool in tools
            if self._planner_name_allowed(
                getattr(tool, "name", None),
                readonly,
                allow,
                anonymous_when_unknown=True,
            )
        ]
        if self._loop._autonomous:
            return planner_tools
        return planner_tools + [
            _ask_user_tool_singleton(),
            _questions_v2_tool_singleton(),
            _clarify_tool_singleton(),
        ]

    def _finish_virtuals(self) -> list:
        from .engine import (  # local: engine owns the compatibility specs
            _finish_alias_tool_spec,
            _finish_tool_singleton,
            _workflow_finish_tool_spec,
        )

        workflow_run = getattr(self._loop, "_workflow_run", None)
        virtuals = [
            _workflow_finish_tool_spec(workflow_run)
            if workflow_run is not None
            else _finish_tool_singleton()
        ]
        if not self._loop._finish_alias:
            return virtuals
        alias = (
            _workflow_finish_tool_spec(
                workflow_run,
                name=self._loop._finish_alias,
            )
            if workflow_run is not None
            else _finish_alias_tool_spec(self._loop._finish_alias)
        )
        virtuals.append(alias)
        return virtuals

    def _execution_virtuals(self, suppress_meta_tools: bool) -> list:
        from .engine import (  # local: engine owns compatibility singletons
            _delegate_explore_tool_singleton,
            _remember_tool_singleton,
            _serve_tool_singleton,
        )

        virtuals = self._finish_virtuals()
        if suppress_meta_tools:
            return virtuals
        if not self._loop._autonomous:
            virtuals += [
                _ask_user_tool_singleton(),
                _clarify_tool_singleton(),
            ]
        virtuals += [
            _propose_plan_update_tool_singleton(),
            _notify_user_tool_singleton(),
            _remember_tool_singleton(),
            _serve_tool_singleton(),
        ]
        if "_run_fanout" in vars(self._loop):
            virtuals.append(_delegate_explore_tool_singleton())
        return virtuals

    def _execution_tools(
        self,
        tools: list,
        *,
        suppress_meta_tools: bool,
        blocked_tools: frozenset[str],
    ) -> list:
        if self._loop._planning_tools:
            plan_name = getattr(
                self._loop._plan_tool,
                "name",
                self._loop._plan_tool,
            )
            tools = [tool for tool in tools if getattr(tool, "name", None) != plan_name]
        combined = list(tools) + self._execution_virtuals(suppress_meta_tools)
        return [tool for tool in combined if getattr(tool, "name", None) not in blocked_tools]

    def tools_for_step(
        self,
        *,
        suppress_meta_tools: bool = False,
        force_submit_only: bool = False,
        force_read_tools: frozenset[str] | None = None,
        blocked_tools: frozenset[str] = frozenset(),
        mode: OperatingMode | None = None,
        available_tools: list | None = None,
    ) -> list:
        effective_mode = mode or self._loop.mode
        tools = [
            tool
            for tool in self._available_tools(available_tools)
            if getattr(tool, "name", None) not in blocked_tools
        ]
        if effective_mode == OperatingMode.PLANNING:
            return self._planning_tools(
                tools,
                force_submit_only=force_submit_only,
                force_read_tools=force_read_tools,
            )
        return self._execution_tools(
            tools,
            suppress_meta_tools=suppress_meta_tools,
            blocked_tools=blocked_tools,
        )

    def _executor_callable_tool_names(self) -> set[str]:
        callable_names = getattr(
            self._loop.executor,
            "callable_tool_names",
            None,
        )
        if callable(callable_names):
            return set(cast("Iterable[str]", callable_names()))
        return {tool.name for tool in self._loop.executor.available_tools()}

    def _virtual_tool_names(self) -> set[str]:
        names = {
            "ask_user",
            "questions_v2",
            "clarify",
            "propose_plan_update",
            "plan_step",
            "notify_user",
            "finish",
            "remember",
            "serve",
            self._loop._plan_tool,
            "delegate_explore",
        }
        if self._loop._finish_alias:
            names.add(self._loop._finish_alias)
        return names | set(self._loop._planning_tools)

    def known_tool_names_for_requery(self) -> set[str]:
        known_names = getattr(
            self._loop.executor,
            "known_tool_names_for_requery",
            None,
        )
        executor_names = (
            set(cast("Iterable[str]", known_names()))
            if callable(known_names)
            else self._executor_callable_tool_names()
        )
        return executor_names | self._virtual_tool_names()

    def allowed_tool_names_for_mode(
        self,
        mode: OperatingMode,
        *,
        available_tools: list,
        blocked_tools: frozenset[str] = frozenset(),
    ) -> set[str]:
        if mode == OperatingMode.PLANNING:
            return set(self.planning_allowed_tool_names(available_tools)) - blocked_tools
        allowed = self._executor_callable_tool_names() | self._virtual_tool_names()
        plan_name = getattr(
            self._loop._plan_tool,
            "name",
            self._loop._plan_tool,
        )
        allowed.difference_update({plan_name, "questions_v2", "plan_step"})
        allowed.difference_update(blocked_tools)
        return allowed

    def unknown_tool_requery_hint(
        self,
        tool_name: str,
        offered_names: set[str],
    ) -> str:
        hint = f"ERROR: Unknown tool '{tool_name}'. Available: {sorted(list(offered_names))}"
        if not self._loop._assist:
            return hint
        suggestion = _nearest_tool_name(tool_name, offered_names)
        return f"{hint} did you mean '{suggestion}'?" if suggestion is not None else hint
