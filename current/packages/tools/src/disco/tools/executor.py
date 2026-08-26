"""The executor — fulfills the loop's `ToolExecutor` (tool-sandbox-contract.md §4).

One call in, one result out. The loop calls `execute()` per proposed action and
`available_tools()` to tell the model what it may call; it is unaware of
sandboxing. The load-bearing guarantee: `execute()` ALWAYS returns a ToolResult
(success or a structured, model-readable failure) and NEVER raises to the loop —
that's what makes the loop's "exactly one observation per action" hold, and what
turns malformed tool calls into the auto-repair loop (§3).

This module owns the effect boundary only. The cohesive interior pieces live in
`executor_parts`: argument introspection and repair, the model-facing validation
message, the read-only tool catalog, and the admission ladder/result shapes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from disco.core import Event, ToolCall, ToolResult
from disco.core.appkit.primitives import PrimitiveLiveVerifier
from disco.core.contract import ContractScopeGuard
from disco.core.effects import ActionProfile, EffectCapability
from disco.core.llm import CompletionRequest, CompletionResponse, ModelExecutionPolicy, ToolSpec
from pydantic import BaseModel

from .anatomy import Tool, ToolContext, ToolDef, ToolOutcome
from .builtin.files import clear_conversation_read_state, mark_read
from .executor_parts.arguments import normalize_list_item_wrappers
from .executor_parts.catalog import ToolCatalog
from .executor_parts.invocation import (
    ExecutionSuperseded,
    admit_call,
    classify_invocation_error,
    fail_result,
    success_result,
    validated_receipts,
)
from .registry import ToolRegistry, ToolScope
from .release_intent import ReleaseIntentWriter
from .sandbox.base import SandboxInstance
from .secrets import CapabilityBroker

# Module-level singleton used as the default for model_policy in DefaultToolExecutor
# (ruff B008 forbids function calls in default args; frozen dataclass is safe as a singleton).
_STANDARD_POLICY: ModelExecutionPolicy = ModelExecutionPolicy.standard()
_EXECUTION_AGENT_VIEW_ID: ContextVar[str | None] = ContextVar(
    "disco_execution_agent_view_id", default=None
)
_EXECUTION_FENCE_HELD: ContextVar[bool] = ContextVar("disco_execution_fence_held", default=False)


class DefaultToolExecutor:
    """[CONTRACT behavior; INTERIOR code] The concrete `ToolExecutor`.

    The context is built internally from injected pieces (sandbox, capability
    broker, ownership) rather than a free `ctx_factory` — an [INTERIOR] choice;
    the §4 illustrative `ctx_factory` is one way to spell the same thing.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        scope: ToolScope,
        *,
        sandbox: SandboxInstance | None = None,
        broker: CapabilityBroker | None = None,
        owner_id: str = "local",
        conversation_id: str | None = None,
        default_timeout_s: int = 300,
        model_policy: ModelExecutionPolicy = _STANDARD_POLICY,
        driver_llm: tuple[str, str, str | None] | None = None,
        provider_completion: Callable[[CompletionRequest], Awaitable[CompletionResponse]]
        | None = None,
        source_report: str | None = None,
        read_char_budget: int | None = None,
        scope_guard: ContractScopeGuard | None = None,
        on_tool_success: Callable[[str], None] | None = None,
        starter_kit: str | None = None,
        workflow_events: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        primitive_live_verifier: PrimitiveLiveVerifier | None = None,
        workspace_lock: asyncio.Lock | None = None,
        workspace_fence: Callable[[], AbstractAsyncContextManager[None]] | None = None,
        execution_admission: Callable[[str | None], Awaitable[str | None]] | None = None,
        release_intent_writer: ReleaseIntentWriter | None = None,
        prepare_for_events: Callable[[list[Event]], Awaitable[None]] | None = None,
        plan_submission_guard: Callable[[list[Event]], str | None] | None = None,
    ) -> None:
        self._registry = registry
        self._scope = scope
        # The declared action space is a read-only projection, not an effect: the
        # catalog receives the LIVE scope on every call so a phase-aware subclass
        # (ScopedPhaseExecutor/AppKitToolExecutor, whose `_scope` is a property)
        # keeps enforcing its current allowlist.
        self._catalog = ToolCatalog(registry)
        self._sandbox = sandbox
        # CONTRACT-ENFORCE: optional per-phase contract scope guard. When a Build run
        # is governed by a BuildContract, this denies a tool call that is out of the
        # current phase's allowlist (e.g. file_write during an appkit EDIT phase).
        # None ⇒ a plain agent run ⇒ no contract enforcement (unchanged behavior).
        self._scope_guard = scope_guard
        # CONTRACT-ACTIVATE: notified with the tool name after each SUCCESSFUL call so a
        # build-phase tracker can advance (bootstrap-tool success → edit; finalizer →
        # verify). None ⇒ no tracking. Best-effort: a callback error never fails the call.
        self._on_tool_success = on_tool_success
        # P7: the active contract's starter_kit name, stamped on every ToolContext so
        # scaffold_starter materializes THIS build's starter. None ⇒ no contract starter.
        self._starter_kit = starter_kit
        self._workflow_events = workflow_events
        self._primitive_live_verifier = primitive_live_verifier
        # K6d — one runtime-owned lock serializes every operation that can touch
        # a conversation workspace with host-side final capture.  We guard the
        # actual tool invocation (including context construction) rather than
        # trusting ``read_only`` or a capability classifier: shell, MCP, and
        # future opaque tools can have workspace effects that metadata cannot
        # prove exhaustively.  Waiting for the lock is deliberately outside the
        # tool timeout; contention is not a tool failure.
        self._workspace_lock = workspace_lock
        self._workspace_fence = workspace_fence
        self._execution_admission = execution_admission
        # WO-C1: the host-owned release-intent writer stamped onto every ToolContext
        # so release_declare (in_process) persists under the ACTIVE configured store.
        # None ⇒ no host writer wired (standalone executor) ⇒ the tool fails closed.
        self._release_intent_writer = release_intent_writer
        self._prepare_for_events = prepare_for_events
        self._plan_submission_guard = plan_submission_guard
        # ROOT-5: the conversation's effective (override-aware) driver endpoint,
        # stamped onto every ToolContext for LLM-using tools (slides_generate).
        self._driver_llm = driver_llm
        self._provider_completion = provider_completion
        self._source_report = source_report
        # CW-6: the capability-derived file_read page budget, stamped onto every
        # ToolContext so files.py can read a file that fits the snapshot pin in one
        # shot. None ⇒ files.py uses its static default (assist-ON parity).
        self._read_char_budget = read_char_budget
        self._broker = broker or CapabilityBroker()
        self._owner_id = owner_id
        # Generate a unique per-instance id when none is given so the F3 read-state
        # tracker never silently shares a bucket with another executor (the old "conv"
        # default would have all unkeyed executors share one bucket).
        self._conversation_id = (
            conversation_id if conversation_id is not None else f"conv_{uuid4().hex}"
        )
        self._default_timeout_s = default_timeout_s
        self._model_policy = model_policy  # replaces bare assist: bool; standard = no-op
        self._killed = False
        # BF1: one target-agnostic coherence clock per executor/conversation.
        # Zero is kept host-side and omitted from the browser wire; wire epochs
        # are exact positive integers.  The random generation prevents a newly
        # created executor from inheriting a prior executor's daemon state.
        self._workspace_mutation_epoch = 0
        self._browser_generation = uuid4().hex

    sandbox = property(lambda self: self._sandbox)

    async def prepare_for_events(self, events: list[Event]) -> None:
        """Run one optional host-owned workspace preparation hook.

        The loop calls this immediately before rendering a model view and again
        under the execution fence before an effect.  A no-op default keeps the
        executor contract simple while allowing immutable host inputs to be
        restored before the model or a tool can observe the workspace.
        """

        if self._prepare_for_events is not None:
            await self._prepare_for_events(events)

    def plan_submission_refusal(self, events: list[Event]) -> str | None:
        """Return one host-owned reason that the current plan must be retried."""

        if self._plan_submission_guard is None:
            return None
        try:
            return self._plan_submission_guard(events)
        except Exception:  # noqa: BLE001 — a failed host precondition fails closed
            return (
                "Plan submission is temporarily blocked because the host could not "
                "validate its required workspace inputs. Retry after re-reading the "
                "required inputs."
            )

    # ---- the ToolExecutor protocol ------------------------------------------

    @asynccontextmanager
    async def _workspace_execution_fence(self) -> AsyncIterator[None]:
        if self._workspace_lock is None:
            if self._workspace_fence is None:
                yield
            else:
                async with self._workspace_fence():
                    yield
            return
        async with self._workspace_lock:
            if self._workspace_fence is None:
                yield
            else:
                async with self._workspace_fence():
                    yield

    async def execute_attributed(
        self,
        call: ToolCall,
        agent_view_id: str | None,
        on_result: Callable[[ToolResult], Awaitable[None]] | None = None,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Execute and persist its paired result inside one generation fence."""

        async with self._workspace_execution_fence():
            view_token = _EXECUTION_AGENT_VIEW_ID.set(agent_view_id)
            fence_token = _EXECUTION_FENCE_HELD.set(True)
            try:
                # Dynamic phase/workspace authority must be refreshed only AFTER
                # acquiring this fence. Otherwise a rollback can land while the
                # call waits and a tool selected under stale scope can execute.
                if prepare is not None:
                    await prepare()
                result = await self.execute(call)
                if on_result is not None:
                    await on_result(result)
                return result
            finally:
                _EXECUTION_FENCE_HELD.reset(fence_token)
                _EXECUTION_AGENT_VIEW_ID.reset(view_token)

    # ---- declared action space (delegated to the catalog) --------------------

    def tool_scope(self, tool_name: str) -> str:
        """'sandbox' | 'in_process' | 'unknown' — where this tool executes.
        Policy input for the blast-radius gate (DC-03)."""
        return self._catalog.tool_scope(tool_name, self._scope)

    def tool_scope_for_call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """The effective pre-execution scope for this concrete tool call."""
        return self._catalog.tool_scope_for_call(tool_name, arguments, self._scope)

    def available_tools(self) -> list[ToolSpec]:
        return self._catalog.available_tools(self._scope, self._model_policy)

    def callable_tool_names(self) -> frozenset[str]:
        """Names of every tool the executor will actually run (registry ∩
        allowed_tools), IGNORING advertised_tools."""
        return self._catalog.callable_tool_names(self._scope)

    known_tool_names_for_requery = callable_tool_names

    def readonly_tool_names(self) -> frozenset[str]:
        """Names of in-scope tools that only OBSERVE (ToolDef.read_only)."""
        return self._catalog.readonly_tool_names(self._scope)

    def action_profile_for_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ActionProfile | None:
        """The validated shadow action profile for one concrete call, or None."""

        return self._catalog.action_profile_for_call(tool_name, arguments, self._scope)

    # ---- the effect boundary -------------------------------------------------

    async def execute(self, call: ToolCall) -> ToolResult:
        admitted = admit_call(
            call,
            killed=self._killed,
            registry=self._registry,
            scope=self._scope,
            scope_guard=self._scope_guard,
            unknown_tool_message=self._unknown_tool_message,
        )
        if isinstance(admitted, ToolResult):
            return admitted
        tool, args = admitted.tool, admitted.args

        # K1 shadow classification. It does not gate or reroute execution; it
        # only determines whether new typed receipts may be trusted as exact.
        action_profile, profile_error = self._catalog.profile_for_validated_call(tool, args)

        # Invalid classifiers cannot authorize exact receipts, but the broad
        # declared behavior is still durable evidence of what this invocation
        # may have done. This fallback never expands beyond ToolBehavior.
        persisted_profile = action_profile
        if persisted_profile is None and tool.definition.behavior is not None:
            persisted_profile = tool.definition.behavior.static_profile()

        # 3–4. Build context and execute within one failure boundary. Context
        # construction is part of a validated invocation and must not escape the
        # executor or disappear from restart/replay evidence.
        try:
            raw_outcome = await self._invoke(tool, args)
            # The Tool protocol is a runtime trust boundary, not merely a type
            # hint. Revalidate even ToolOutcome instances so model_copy cannot
            # smuggle an invalid result past the mapped failure path.
            outcome = ToolOutcome.model_validate(
                raw_outcome.model_dump() if isinstance(raw_outcome, ToolOutcome) else raw_outcome
            )
        except Exception as e:  # noqa: BLE001 — the tool's own failure is an observation
            failure = classify_invocation_error(
                e,
                tool_def=tool.definition,
                default_timeout_s=self._default_timeout_s,
            )
            if failure.advances_mutation_epoch:
                self._advance_workspace_mutation_epoch(persisted_profile)
            return fail_result(
                call,
                failure.kind,
                failure.message,
                action_profile=persisted_profile,
            )

        if outcome.success:
            self._notify_tool_success(call.tool_name)

        # BF1: advance exactly once after an invocation whose trusted persisted
        # capability profile says it may have mutated the workspace.
        # This intentionally does not inspect tool names, arguments, command
        # strings, receipts, frameworks, or paths. A reported failure can follow
        # a partial mutation, so it invalidates old verification without earning
        # productive-work credit.
        self._advance_workspace_mutation_epoch(persisted_profile)

        return success_result(
            call,
            outcome,
            effect_receipts=validated_receipts(
                outcome.effect_receipts,
                action_profile,
                profile_error,
            ),
            action_profile=persisted_profile,
        )

    # ---- kill switch (§6.4) -------------------------------------------------

    async def kill(self) -> None:
        """Revoke capabilities + egress and destroy the instance (BoD §13.6).
        Subsequent execute() calls fail with `sandbox_error`."""
        self._killed = True
        self._broker.revoke_all()
        if self._sandbox is not None:
            await self._sandbox.destroy()
        # F3 teardown: remove this conversation's read/write tracker entry so the
        # module-level dict doesn't grow unbounded over the lifetime of the process.
        clear_conversation_read_state(self._conversation_id)

    def note_grounding_read(self, path: str) -> None:
        """Record that `path`'s CURRENT content was put in front of the model by a
        grounded, non-tool channel this turn (the CURRENT WORKSPACE snapshot
        pinning it in full, or an F9 read-dedup pointer at the prior read).

        Satisfies the read-before-write gate for that path so a subsequent
        file_write is allowed: the model is NOT writing blind from memory — it has
        the current bytes — yet no ``FileReadTool.run`` fired to set the bit. The
        agent loop calls this from the snapshot builder and the F9 short-circuit.
        Without it, a deduped/snapshotted read leaves the model unable to read
        (only a pointer) and unable to write (gated) — the unrecoverable
        file_write loop. No-op on a killed executor."""
        if self._killed:
            return
        mark_read(self._conversation_id, path)

    # ---- helpers ------------------------------------------------------------

    async def _invoke(self, tool: Tool, args: BaseModel) -> Any:
        """Run an admitted call under the workspace fence, unless one is held."""
        if _EXECUTION_FENCE_HELD.get():
            return await self._invoke_fenced(tool, args)
        async with self._workspace_execution_fence():
            return await self._invoke_fenced(tool, args)

    async def _invoke_fenced(self, tool: Tool, args: BaseModel) -> Any:
        if self._execution_admission is not None:
            refusal = await self._execution_admission(_EXECUTION_AGENT_VIEW_ID.get())
            if refusal is not None:
                raise ExecutionSuperseded(refusal)
        ctx = await self._build_context(tool.definition)
        return await asyncio.wait_for(tool.run(args, ctx), timeout=ctx.timeout_s)

    def _notify_tool_success(self, tool_name: str) -> None:
        # CONTRACT-ACTIVATE: advance the build-phase tracker on a successful call
        # (best-effort — a tracker error must never fail an otherwise-good tool run).
        if self._on_tool_success is None:
            return
        try:
            self._on_tool_success(tool_name)
        except Exception:  # noqa: BLE001 — tracking is advisory, never fatal
            pass

    async def _build_context(self, tool_def: ToolDef) -> ToolContext:
        sandbox = self._sandbox if tool_def.runs_in == "sandbox" else None
        in_sandbox = tool_def.runs_in == "sandbox"
        sessions = getattr(self._sandbox, "sessions", None) if in_sandbox else None

        kernel = None
        if tool_def.runs_in == "sandbox" and self._sandbox is not None:
            # SandboxSession.kernel is a property whose getter is async — accessing
            # it yields a coroutine to await. Raw instances (no kernel attr) -> None.
            kernel_coro = getattr(self._sandbox, "kernel", None)
            if kernel_coro is not None:
                kernel = await kernel_coro

        return ToolContext(
            sandbox=sandbox,
            sessions=sessions,
            kernel=kernel,
            workspace_path=".",  # relative to the sandbox instance's jailed workspace
            # Per-tool override wins (long generative tools declare a bigger budget);
            # else the generic default. Kills the "slides_generate exceeded 300s ->
            # plain deck" failure without loosening the ceiling for every other tool.
            timeout_s=tool_def.timeout_s or self._default_timeout_s,
            capabilities=self._broker.grant(tool_def.uses_capabilities),
            owner_id=self._owner_id,
            conversation_id=self._conversation_id,
            assist=self._model_policy.assist,
            driver_llm=self._driver_llm,
            provider_completion=self._provider_completion,
            source_report=self._source_report,
            read_char_budget=self._read_char_budget,
            starter_kit=self._starter_kit,
            workflow_events=self._workflow_events,
            primitive_live_verifier=self._primitive_live_verifier,
            scope_allowed_tools=self._scope.allowed_tools,
            browser_workspace_epoch=(
                self._workspace_mutation_epoch if self._workspace_mutation_epoch > 0 else None
            ),
            browser_generation=self._browser_generation,
            browser_lane="agent",
            release_intent_writer=self._release_intent_writer,
        )

    def _advance_workspace_mutation_epoch(
        self,
        profile: ActionProfile | None,
    ) -> None:
        if profile is not None and EffectCapability.WORKSPACE_MUTATE in profile.capabilities:
            self._workspace_mutation_epoch += 1

    def _unknown_tool_message(self, tool_name: str, available: list[str]) -> str:
        return f"unknown or out-of-scope tool {tool_name!r}; available: {available}"


def validate_args(tool_def: ToolDef, arguments: dict[str, Any]) -> BaseModel:
    """[CONTRACT helper] Validate raw arguments against a tool's args_model.
    Raises ValidationError; the executor catches it for the repair loop (§3)."""
    return tool_def.args_model.model_validate(
        normalize_list_item_wrappers(tool_def.name, tool_def.args_model, arguments)
    )
