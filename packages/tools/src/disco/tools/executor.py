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
from uuid import uuid4

from disco.core import ToolCall, ToolResult
from disco.core.llm import ModelExecutionPolicy, ToolSpec
from pydantic import BaseModel, ValidationError

from .anatomy import ToolContext, ToolDef, ToolExecutionError
from .builtin.files import clear_conversation_read_state
from .registry import ToolRegistry, ToolScope
from .sandbox.base import SandboxError, SandboxInstance
from .secrets import CapabilityBroker, CapabilityDenied

# Module-level singleton used as the default for model_policy in DefaultToolExecutor
# (ruff B008 forbids function calls in default args; frozen dataclass is safe as a singleton).
_STANDARD_POLICY: ModelExecutionPolicy = ModelExecutionPolicy.standard()

# Friendly names for the common annotations so the model sees "string"/"integer"
# rather than "<class 'str'>". Falls back to the annotation's own __name__.
_TYPE_NAMES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _field_type_name(annotation: Any) -> str:
    """Best-effort friendly name for a pydantic field's annotation."""
    if annotation in _TYPE_NAMES:
        return _TYPE_NAMES[annotation]
    name = getattr(annotation, "__name__", None)
    if isinstance(name, str) and name:
        return name
    # Unions / generics (Optional[str], list[str], ...) — render the typing repr,
    # trimmed of the typing/module noise so it stays readable.
    return str(annotation).replace("typing.", "")


def _valid_arg_keys(args_model: type[BaseModel]) -> set[str]:
    """Every key the model legitimately may pass: field names AND any aliases
    (pydantic accepts a field by either, depending on populate_by_name)."""
    keys: set[str] = set()
    for fname, finfo in args_model.model_fields.items():
        keys.add(fname)
        if finfo.alias:
            keys.add(finfo.alias)
        if finfo.validation_alias and isinstance(finfo.validation_alias, str):
            keys.add(finfo.validation_alias)
    return keys


def _arg_surface(args_model: type[BaseModel]) -> str:
    """Concise one-line summary of a tool's accepted arguments: each as
    `name (type, required|optional)`. Small by construction — tool arg models
    are a handful of fields — so it never dumps a huge schema."""
    parts: list[str] = []
    for fname, finfo in args_model.model_fields.items():
        req = "required" if finfo.is_required() else "optional"
        parts.append(f"{fname} ({_field_type_name(finfo.annotation)}, {req})")
    return ", ".join(parts) if parts else "(takes no arguments)"


def describe_validation_failure(
    tool_name: str,
    args_model: type[BaseModel],
    arguments: dict[str, Any],
    errors: list[dict[str, Any]],
) -> str:
    """Build a SELF-CORRECTING, model-facing validation error.

    The model only sees a failed ToolResult's `error`/`content` string (the
    structured `validation_errors`/`expected_schema` are not surfaced into the
    agent's message stream), so the actionable detail MUST live in this string.
    It names the unexpected key(s) the caller invented (e.g. `cmd`) AND the
    expected/required field(s) it should have used (e.g. `command`), pulled
    generically from the pydantic model — so EVERY tool benefits, not just shell.

    Deterministic for identical arguments (so byte-identical repeated bad calls
    still trip the loop's stuck detector as before).
    """
    valid_keys = _valid_arg_keys(args_model)
    unknown = sorted(k for k in arguments if k not in valid_keys)

    reasons: list[str] = []
    if unknown:
        reasons.append(
            f"unexpected argument(s) {unknown} — not accepted by this tool"
        )
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        etype = err.get("type", "")
        if etype == "missing":
            reasons.append(f"missing required argument {loc!r}")
        elif etype in {"extra_forbidden", "unexpected_keyword_argument"}:
            # already covered by `unknown` above; skip to avoid duplication
            continue
        else:
            reasons.append(f"argument {loc!r}: {err.get('msg', etype)}")
    if not reasons:
        reasons.append("arguments did not match the tool's schema")

    msg = (
        f"arguments for {tool_name!r} failed validation: "
        + "; ".join(reasons)
        + f". Expected arguments: {_arg_surface(args_model)}."
    )
    if unknown:
        # spell out the likely fix so a weaker model can self-correct next turn
        required_names = [
            n for n, fi in args_model.model_fields.items() if fi.is_required()
        ]
        if required_names:
            msg += (
                f" Re-call {tool_name!r} using the correct key(s) "
                f"{required_names} instead of {unknown}."
            )
    if arguments:
        msg += f" You provided: {sorted(arguments)}."
    return msg


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
        read_char_budget: int | None = None,
    ) -> None:
        self._registry = registry
        self._scope = scope
        self._sandbox = sandbox
        # ROOT-5: the conversation's effective (override-aware) driver endpoint,
        # stamped onto every ToolContext for LLM-using tools (slides_generate).
        self._driver_llm = driver_llm
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

    @property
    def sandbox(self) -> SandboxInstance | None:
        """The live sandbox instance, or None for a sandbox-less executor.

        The agent loop reaches the sandbox through this duck-typed accessor
        (getattr(executor, "sandbox", None)) — both for mid-session-recreation
        generation tracking (engine._execute_and_observe) and for re-reading the
        current on-disk workspace each turn (engine._workspace_snapshot_message).
        Exposing it read-only keeps that seam working without leaking _sandbox."""
        return self._sandbox

    # ---- the ToolExecutor protocol ------------------------------------------

    def tool_scope(self, tool_name: str) -> str:
        """'sandbox' | 'in_process' | 'unknown' — where this tool executes.
        Policy input for the blast-radius gate (DC-03)."""
        for tool in self._registry.in_scope(self._scope):
            if tool.definition.name == tool_name:
                return tool.definition.runs_in
        return "unknown"

    def available_tools(self) -> list[ToolSpec]:
        tools = self._registry.in_scope(self._scope)  # registry ∩ allowed_tools
        if self._scope.advertised_tools is not None:
            tools = [t for t in tools if t.definition.name in self._scope.advertised_tools]
        # Contract #3: additionally drop any tool the model policy withholds, regardless
        # of what the scope advertises.  For the standard tier withheld_tools is empty
        # (frozenset()) so this branch is a no-op.  For the weak tier and !anchored_edit
        # this is the executor-level enforcement backstop on top of the registry's scope.
        withheld = self._model_policy.withheld_tools
        if withheld:
            tools = [t for t in tools if t.definition.name not in withheld]
        return [t.definition.to_spec() for t in tools]

    def callable_tool_names(self) -> frozenset[str]:
        """Names of every tool the executor will actually run (registry ∩
        allowed_tools), IGNORING advertised_tools. The advertise/callable split
        (RP-05c) hides over-cap MCP tools from available_tools(), but they remain
        callable by qualified name — callers that need to know 'can this name be
        executed?' (e.g. the engine's unknown-tool requery gate) must use THIS,
        not available_tools(), or they will bounce withheld-but-callable tools."""
        return frozenset(t.definition.name for t in self._registry.in_scope(self._scope))

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
            errors = [dict(err) for err in e.errors()]  # ErrorDetails -> plain dict
            return self._fail(
                call,
                "invalid_arguments",
                # SELF-CORRECTING message: names the unexpected key(s) the model
                # invented AND the expected/required field(s). The model only sees
                # this string (not `structured`), so the fix lives here, generic
                # across all tools — un-sticks the 5-failure gate on bad arg keys.
                describe_validation_failure(
                    call.tool_name,
                    tool.definition.args_model,
                    call.arguments if isinstance(call.arguments, dict) else {},
                    errors,
                ),
                validation_errors=errors,
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
            # DEFECT-2: tools report failure via content, not error; fall back so
            # AgentErrorEvent receives the diagnosis instead of bare "tool failed".
            error=outcome.error or (None if outcome.success else outcome.content),
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
            assist=self._model_policy.assist,
            driver_llm=self._driver_llm,
            read_char_budget=self._read_char_budget,
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
