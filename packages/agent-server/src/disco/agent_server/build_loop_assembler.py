"""Final Build-like AgentLoop assembly and observation."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from disco.core import LLMSummarizingCondenser, SecurityRisk, StatusEvent, VerifierVerdictEvent
from disco.core.build_platform import ObserveOnlyShadowRecord
from disco.core.llm import DefaultLLMRouter, OperatingMode, RouterSummarizer
from disco.core.loop import (
    AgentLoop,
    BlastRadiusConfirm,
    RouterAgent,
    SealabilityProbe,
    host_verify_authoritative_enabled,
)
from disco.core.security import RuleBasedAnalyzer
from disco.core.workflow import WorkflowRun
from disco.tools import WORKFLOW_ROUTER_ALLOWED_TOOLS, AppKitPhase, DefaultToolExecutor

from .build_loop_components import (
    ComposeContext,
    LoopBuildPlatformPort,
    LoopContractPort,
    LoopEventStorePort,
    LoopSettingsPort,
    LoopWorkspacePort,
)
from .build_platform_shadow import (
    build_platform_shadow_enabled,
    observe_legacy_build,
)
from .verify.dispatcher import HostVerifierDispatcher
from .verify.host import HostWebAppVerifier
from .verify.model_verifier import ModelVerifier

logger = logging.getLogger(__name__)

_PLANNING_TOOLS = frozenset(
    {"submit_plan", "file_list", "file_read", "search", "extract", "think"}
)


class BuildShadowLedger:
    """Own legacy-composition shadow observations without exposing mutation."""

    def __init__(self) -> None:
        self._records: dict[str, ObserveOnlyShadowRecord] = {}

    def record(self, conversation_id: str, record: ObserveOnlyShadowRecord) -> None:
        self._records[conversation_id] = record

    def snapshot(self) -> dict[str, ObserveOnlyShadowRecord]:
        return dict(self._records)


@dataclass(frozen=True, slots=True)
class _LoopRuntime:
    conversation_id: str
    router: DefaultLLMRouter
    agent: RouterAgent
    executor: DefaultToolExecutor
    store: LoopEventStorePort
    mcp_risks: dict[str, SecurityRisk]


@dataclass(frozen=True, slots=True)
class _LoopPresentation:
    finish_alias: str | None
    quiet: bool
    autonomous: bool
    context_window: int | None


@dataclass(frozen=True, slots=True)
class _LoopHooks:
    host_verifier: HostVerifierDispatcher
    verifier_judge: ModelVerifier
    verifier_hook: Callable[[VerifierVerdictEvent], Awaitable[None]] | None
    authoritative: bool
    terminal_hook: Callable[[StatusEvent], Awaitable[StatusEvent]]
    seal_probe: SealabilityProbe
    fence: Callable[[], AbstractAsyncContextManager[None]]


class BuildLoopAssembler:
    """Construct one loop after all mutable resources are owned elsewhere."""

    def __init__(
        self,
        settings: LoopSettingsPort,
        workspace: LoopWorkspacePort,
        event_store: LoopEventStorePort,
        contract: LoopContractPort,
        build_platform: LoopBuildPlatformPort,
        shadows: BuildShadowLedger,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._event_store = event_store
        self._contract = contract
        self._build_platform = build_platform
        self._shadows = shadows

    def assemble(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        agent: RouterAgent,
        executor: DefaultToolExecutor,
        context: ComposeContext,
        sealed_workflow_run: WorkflowRun | None,
        mcp_risks: dict[str, SecurityRisk],
    ) -> AgentLoop:
        finish_alias = self._finish_alias(conversation_id, sealed_workflow_run)
        self._set_finish_alias(agent, finish_alias)
        self._observe_composition(
            conversation_id,
            executor,
            context,
            sealed=sealed_workflow_run is not None,
        )
        runtime = _LoopRuntime(
            conversation_id=conversation_id,
            router=router,
            agent=agent,
            executor=executor,
            store=self._event_store,
            mcp_risks=mcp_risks,
        )
        presentation = _LoopPresentation(
            finish_alias=finish_alias,
            quiet=self._settings._effective_quiet(conversation_id),
            autonomous=self._settings._effective_autonomous(conversation_id),
            context_window=context.policy.driver_context_window,
        )
        hooks = self._hooks(conversation_id, router, executor)
        if sealed_workflow_run is not None:
            return self._sealed_loop(
                runtime,
                presentation,
                hooks,
                context,
                sealed_workflow_run,
            )
        if context.modes.art_mode:
            return self._artifact_loop(runtime, presentation, hooks, context)
        return self._standard_loop(runtime, presentation, hooks, context)

    def _finish_alias(
        self,
        conversation_id: str,
        sealed_workflow_run: WorkflowRun | None,
    ) -> str | None:
        if sealed_workflow_run is not None:
            return sealed_workflow_run.definition.verify.finalizer
        return self._contract._finalizer_alias_for(conversation_id)

    @staticmethod
    def _set_finish_alias(agent: RouterAgent, finish_alias: str | None) -> None:
        setter = getattr(agent, "set_finish_alias", None)
        if callable(setter):
            setter(finish_alias)

    def _observe_composition(
        self,
        conversation_id: str,
        executor: DefaultToolExecutor,
        context: ComposeContext,
        *,
        sealed: bool,
    ) -> None:
        modes = context.modes
        if build_platform_shadow_enabled() and not sealed:
            try:
                self._shadows.record(
                    conversation_id,
                    observe_legacy_build(
                        appkit_mode=modes.appkit_mode,
                        tool_specs=executor.available_tools(),
                    ),
                )
            except Exception:
                logger.warning(
                    "build-platform shadow observer failed; legacy authority is unchanged",
                    exc_info=True,
                )
        self._build_platform.select_builtin(
            conversation_id,
            appkit=modes.appkit_mode,
            eligible=not sealed
            and not (modes.art_mode or modes.workflow_router_mode),
            tool_specs=executor.available_tools(),
        )

    def _hooks(
        self,
        conversation_id: str,
        router: DefaultLLMRouter,
        executor: DefaultToolExecutor,
    ) -> _LoopHooks:
        web_verifier = HostWebAppVerifier(executor)
        host_verifier = HostVerifierDispatcher(
            {
                (
                    "disco.host_web_verifier@1",
                    "disco.web_functional@1",
                    "host.verify_deliverable",
                ): web_verifier,
            },
            legacy_adapter=web_verifier,
        )
        return _LoopHooks(
            host_verifier=host_verifier,
            verifier_judge=ModelVerifier(router, conversation_id=conversation_id),
            verifier_hook=self._contract._host_verify_canary_hook_for(
                conversation_id,
                self._contract.note_build_verify_result,
            ),
            authoritative=host_verify_authoritative_enabled(),
            terminal_hook=self._workspace.terminal_commit_hook(conversation_id),
            seal_probe=self._workspace.finish_sealability_probe(conversation_id),
            fence=lambda: self._workspace.fence(conversation_id),
        )

    @staticmethod
    def _common_kwargs(
        presentation: _LoopPresentation,
        hooks: _LoopHooks,
        context: ComposeContext,
    ) -> dict[str, Any]:
        return {
            "autonomous": presentation.autonomous,
            "model_policy": context.policy.model_policy,
            "driver_context_window": presentation.context_window,
            "quiet": presentation.quiet,
            "finish_alias": presentation.finish_alias,
            "host_verifier": hooks.host_verifier,
            "verifier_judge": hooks.verifier_judge,
            "host_verifier_verdict_hook": hooks.verifier_hook,
            "host_verify_authoritative": hooks.authoritative,
            "terminal_commit_hook": hooks.terminal_hook,
            "finish_sealability_probe": hooks.seal_probe,
            "control_fence": hooks.fence,
        }

    def _sealed_loop(
        self,
        runtime: _LoopRuntime,
        presentation: _LoopPresentation,
        hooks: _LoopHooks,
        context: ComposeContext,
        sealed_workflow_run: WorkflowRun,
    ) -> AgentLoop:
        phase = context.modes.workflow_phase
        assert phase is not None
        assert phase.compiled_run_scope is not None
        plan_gated = "submit_plan" in phase.compiled_run_scope.allowed_tools
        common = self._common_kwargs(presentation, hooks, context)
        common["autonomous"] = True
        return self._new_loop(
            runtime,
            presentation,
            mode=OperatingMode.PLANNING if plan_gated else OperatingMode.INTERACTIVE,
            planning_tools=(
                _PLANNING_TOOLS & phase.compiled_run_scope.allowed_tools
                if plan_gated
                else frozenset()
            ),
            workflow_run=sealed_workflow_run,
            common=common,
        )

    def _artifact_loop(
        self,
        runtime: _LoopRuntime,
        presentation: _LoopPresentation,
        hooks: _LoopHooks,
        context: ComposeContext,
    ) -> AgentLoop:
        return self._new_loop(
            runtime,
            presentation,
            mode=OperatingMode.INTERACTIVE,
            planning_tools=frozenset(),
            common=self._common_kwargs(presentation, hooks, context),
        )

    def _standard_loop(
        self,
        runtime: _LoopRuntime,
        presentation: _LoopPresentation,
        hooks: _LoopHooks,
        context: ComposeContext,
    ) -> AgentLoop:
        modes = context.modes
        risks = dict(runtime.mcp_risks)
        if modes.appkit_mode:
            risks["request_custom_build"] = SecurityRisk.HIGH
        adjusted = _LoopRuntime(
            conversation_id=runtime.conversation_id,
            router=runtime.router,
            agent=runtime.agent,
            executor=runtime.executor,
            store=runtime.store,
            mcp_risks=risks,
        )
        common = self._common_kwargs(presentation, hooks, context)
        if modes.appkit_phase is not None:
            common["strict_appkit_active"] = (
                lambda phase=modes.appkit_phase: phase.phase != AppKitPhase.CUSTOM_BUILD
            )
        planning_tools = _PLANNING_TOOLS
        if modes.workflow_router_mode:
            planning_tools |= WORKFLOW_ROUTER_ALLOWED_TOOLS
        return self._new_loop(
            adjusted,
            presentation,
            mode=OperatingMode.PLANNING,
            planning_tools=planning_tools,
            common=common,
        )

    @staticmethod
    def _new_loop(
        runtime: _LoopRuntime,
        presentation: _LoopPresentation,
        *,
        mode: OperatingMode,
        planning_tools: frozenset[str],
        common: dict[str, Any],
        workflow_run: WorkflowRun | None = None,
    ) -> AgentLoop:
        return AgentLoop(
            runtime.conversation_id,
            runtime.store,
            runtime.agent,
            runtime.executor,
            runtime.router,
            RuleBasedAnalyzer(runtime.mcp_risks),
            BlastRadiusConfirm(),
            LLMSummarizingCondenser(context_window=presentation.context_window),
            RouterSummarizer(runtime.router),
            mode=mode,
            planning_tools=planning_tools,
            workflow_run=workflow_run,
            **common,
        )
