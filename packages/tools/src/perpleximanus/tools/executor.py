"""The executor — fulfills the loop's `ToolExecutor` (tool-sandbox-contract.md §4).

One call in, one result out. The loop calls `execute()` per proposed action and
`available_tools()` to tell the model what it may call; it is unaware of
sandboxing. The load-bearing guarantee: `execute()` ALWAYS returns a ToolResult
(success or a structured, model-readable failure) and NEVER raises to the loop —
that's what makes the loop's "exactly one observation per action" hold, and what
turns malformed tool calls into the auto-repair loop (§3).
"""

from __future__ import annotations

import asyncio
from typing import Any

from perpleximanus.core import ToolCall, ToolResult
from perpleximanus.core.llm import ToolSpec
from pydantic import BaseModel, ValidationError

from .anatomy import ToolContext, ToolDef, ToolExecutionError
from .registry import ToolRegistry, ToolScope
from .sandbox.base import SandboxError, SandboxInstance
from .secrets import CapabilityBroker, CapabilityDenied


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
        conversation_id: str = "conv",
        default_timeout_s: int = 300,
    ) -> None:
        self._registry = registry
        self._scope = scope
        self._sandbox = sandbox
        self._broker = broker or CapabilityBroker()
        self._owner_id = owner_id
        self._conversation_id = conversation_id
        self._default_timeout_s = default_timeout_s
        self._killed = False

    # ---- the ToolExecutor protocol ------------------------------------------

    def available_tools(self) -> list[ToolSpec]:
        return [t.definition.to_spec() for t in self._registry.in_scope(self._scope)]

    def readonly_tool_names(self) -> frozenset[str]:
        """Names of in-scope tools that only OBSERVE (ToolDef.read_only). The loop
        consults this to scope the PLANNING agent to read-only tools — a
        capability-level backstop to any name allowlist, so a misconfigured
        allowlist can't leak a write/exec tool to the planner (planner safety)."""
        return frozenset(
            t.definition.name
            for t in self._registry.in_scope(self._scope)
            if t.definition.read_only
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        if self._killed:
            return self._fail(call, "sandbox_error", "executor killed; instance revoked")

        # 1. resolve tool (unknown OR out-of-scope → unknown_tool, never execute)
        tool = self._registry.get(call.tool_name, scope=self._scope)
        if tool is None:
            available = sorted(self._scope.allowed_tools & self._registry.names())
            return self._fail(
                call,
                "unknown_tool",
                f"unknown or out-of-scope tool {call.tool_name!r}; available: {available}",
            )

        # 2. validate args (invalid_arguments with schema; never coerce/execute)
        try:
            args = tool.definition.args_model.model_validate(call.arguments)
        except ValidationError as e:
            return self._fail(
                call,
                "invalid_arguments",
                f"arguments for {call.tool_name!r} failed validation",
                validation_errors=[dict(err) for err in e.errors()],  # ErrorDetails -> plain dict
                expected_schema=tool.definition.args_model.model_json_schema(),
            )

        # 3. build context (sandbox handle + scoped capabilities; NO secrets)
        ctx = await self._build_context(tool.definition)

        # 4. execute with a timeout; map every failure mode to a failed ToolResult
        try:
            outcome = await asyncio.wait_for(tool.run(args, ctx), timeout=ctx.timeout_s)
        except TimeoutError:
            return self._fail(call, "timeout", f"exceeded {ctx.timeout_s}s")
        except CapabilityDenied as e:
            return self._fail(call, "denied", f"capability not granted: {e}")
        except SandboxError as e:
            return self._fail(call, "sandbox_error", str(e))
        except Exception as e:  # noqa: BLE001 — the tool's own failure is an observation
            return self._fail(call, "execution_error", str(e))

        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=outcome.success,
            content=outcome.content,
            structured=outcome.structured,
            error=outcome.error,
        )

    # ---- kill switch (§6.4) -------------------------------------------------

    async def kill(self) -> None:
        """Revoke capabilities + egress and destroy the instance (BoD §13.6).
        Subsequent execute() calls fail with `sandbox_error`."""
        self._killed = True
        self._broker.revoke_all()
        if self._sandbox is not None:
            await self._sandbox.destroy()

    # ---- helpers ------------------------------------------------------------

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
            timeout_s=self._default_timeout_s,
            capabilities=self._broker.grant(tool_def.uses_capabilities),
            owner_id=self._owner_id,
            conversation_id=self._conversation_id,
        )

    def _fail(
        self,
        call: ToolCall,
        kind: Any,
        message: str,
        *,
        validation_errors: list[dict[str, Any]] | None = None,
        expected_schema: dict[str, Any] | None = None,
    ) -> ToolResult:
        err = ToolExecutionError(
            kind=kind,
            message=message,
            validation_errors=validation_errors,
            expected_schema=expected_schema,
        )
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            success=False,
            content=message,
            structured=err.model_dump(mode="json"),
            error=message,
        )


def validate_args(tool_def: ToolDef, arguments: dict[str, Any]) -> BaseModel:
    """[CONTRACT helper] Validate raw arguments against a tool's args_model.
    Raises ValidationError; the executor catches it for the repair loop (§3)."""
    return tool_def.args_model.model_validate(arguments)
