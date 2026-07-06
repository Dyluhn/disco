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
import json
import logging
import types
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Union, get_args, get_origin
from uuid import uuid4

from disco.core import ToolCall, ToolResult
from disco.core.contract import ContractScopeGuard
from disco.core.events import find_elided_arg_markers
from disco.core.llm import ModelExecutionPolicy, ToolSpec
from pydantic import BaseModel, ValidationError

from .anatomy import ToolContext, ToolDef, ToolExecutionError
from .builtin.files import clear_conversation_read_state, mark_read
from .registry import ToolRegistry, ToolScope
from .sandbox.base import SandboxError, SandboxInstance
from .secrets import CapabilityBroker, CapabilityDenied

_LOG = logging.getLogger("disco.tools.executor")

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


def _list_annotation(annotation: Any) -> bool:
    ann = _unwrap_optional(annotation)
    return ann is list or get_origin(ann) is list


def _argument_keys_for_field(field_name: str, field_info: Any) -> tuple[str, ...]:
    keys: list[str] = [field_name]
    if isinstance(field_info.alias, str):
        keys.append(field_info.alias)
    if isinstance(field_info.validation_alias, str):
        keys.append(field_info.validation_alias)
    return tuple(dict.fromkeys(keys))


def normalize_list_item_wrappers(
    tool_name: str,
    args_model: type[BaseModel],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Repair provider dialects that encode list fields as {"item": [...]}."""

    if not isinstance(arguments, dict):
        # A provider dialect can emit LIST-shaped arguments; validation must see
        # them (and refuse with its own message), not crash the run task.
        _LOG.warning("non-dict arguments for %s: %s", tool_name, type(arguments).__name__)
        return arguments
    normalized: dict[str, Any] | None = None
    unwrapped: list[str] = []
    for field_name, field_info in args_model.model_fields.items():
        if not _list_annotation(field_info.annotation):
            continue
        for key in _argument_keys_for_field(field_name, field_info):
            value = arguments.get(key)
            if not isinstance(value, dict) or len(value) != 1:
                continue
            wrapper_key, wrapper_value = next(iter(value.items()))
            if wrapper_key not in {"item", "items"} or not isinstance(wrapper_value, list):
                continue
            if normalized is None:
                normalized = dict(arguments)
            normalized[key] = wrapper_value
            unwrapped.append(key)
    if normalized is None:
        return arguments
    _LOG.debug("unwrapped list item wrapper(s) for %s: %s", tool_name, sorted(unwrapped))
    return normalized


def _arg_surface(args_model: type[BaseModel]) -> str:
    """Concise one-line summary of a tool's accepted arguments: each as
    `name (type, required|optional)`. Small by construction — tool arg models
    are a handful of fields — so it never dumps a huge schema."""
    parts: list[str] = []
    for fname, finfo in args_model.model_fields.items():
        req = "required" if finfo.is_required() else "optional"
        parts.append(f"{fname} ({_field_type_name(finfo.annotation)}, {req})")
    return ", ".join(parts) if parts else "(takes no arguments)"


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` / `Optional[X]` -> `X` (the single non-None member). Leaves any
    other annotation untouched. Lets the nested-shape hint see through an optional
    nested-model field."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _base_type(annotation: Any) -> type | None:
    """The plain python type behind a (possibly Optional) annotation, or None when
    it is not a bare type (a Literal, a generic, a model, ...)."""
    ann = _unwrap_optional(annotation)
    return ann if isinstance(ann, type) else None


def _nested_model(annotation: Any) -> tuple[type[BaseModel], bool] | None:
    """If `annotation` is a pydantic model — or a list/tuple/set of one — return
    `(model, is_list)`; otherwise None. This is what lets the validation error show
    the EXPECTED nested shape (e.g. `steps: list[PlanProgressItem]`) for ANY tool,
    not just update_plan_progress."""
    ann = _unwrap_optional(annotation)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann, False
    if get_origin(ann) in (list, tuple, set, frozenset):
        for arg in get_args(ann):
            inner = _unwrap_optional(arg)
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return inner, True
    return None


def _model_shape(model: type[BaseModel]) -> str:
    """A concise one-line shape for a nested model: each field as `"name": <type>`,
    with a Literal field spelled out as its allowed values (`"state":
    "pending"|"active"|"done"`). Deterministic (model_fields order is stable)."""
    parts: list[str] = []
    for fname, finfo in model.model_fields.items():
        ann = finfo.annotation
        if get_origin(ann) is Literal:
            allowed = "|".join(json.dumps(v) for v in get_args(ann))
            parts.append(f'"{fname}": {allowed}')
        else:
            parts.append(f'"{fname}": <{_field_type_name(ann)}>')
    return "{" + ", ".join(parts) + "}"


def _model_example_item(model: type[BaseModel], i: int) -> dict[str, Any]:
    """One concrete, schema-VALID example object for `model`. `i` varies values
    across items so a list example reads as a list of distinct objects: int fields
    count 1,2,…; a Literal cycles through its allowed values."""
    obj: dict[str, Any] = {}
    for fname, finfo in model.model_fields.items():
        ann = finfo.annotation
        if get_origin(ann) is Literal:
            allowed = list(get_args(ann))
            obj[fname] = allowed[i % len(allowed)]
        else:
            base = _base_type(ann)
            if base is bool:
                obj[fname] = True
            elif base is int:
                obj[fname] = i + 1
            elif base is float:
                obj[fname] = 0.0
            else:
                obj[fname] = "..."
    return obj


def _nested_shape_hint(field_name: str, model: type[BaseModel], is_list: bool) -> str:
    """A concise, copyable hint: the expected nested shape + ONE concrete example —
    so a model that mis-formats an array-of-objects (e.g. `steps: ["", "", ""]`)
    can see exactly what to send instead. Never the full JSON schema (kept small)."""
    shape = _model_shape(model)
    if is_list:
        example = json.dumps([_model_example_item(model, 0), _model_example_item(model, 1)])
        return (
            f"The {field_name!r} argument must be a list of objects, each shaped "
            f"{shape}. Example: {field_name}={example}."
        )
    example = json.dumps(_model_example_item(model, 0))
    return (
        f"The {field_name!r} argument must be an object shaped {shape}. "
        f"Example: {field_name}={example}."
    )


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
    # Nested-shape hints (GENERIC, all tools): when an error points at a field
    # whose value is a model — or a list of models — but the model sent the wrong
    # shape (e.g. `steps: ["", "", ""]` where each item must be an object), append
    # the EXPECTED nested shape + one concrete example so the call is recoverable.
    # Skip "missing"/unknown-key errors (already explained above) and dedupe by
    # field so three bad list items don't repeat the same hint thrice. Deterministic.
    seen_fields: set[str] = set()
    for err in errors:
        loc = err.get("loc", ())
        if not loc:
            continue
        top = loc[0]
        etype = err.get("type", "")
        if (
            not isinstance(top, str)
            or top in seen_fields
            or etype in {"missing", "extra_forbidden", "unexpected_keyword_argument"}
        ):
            continue
        finfo = args_model.model_fields.get(top)
        if finfo is None:
            continue
        nested = _nested_model(finfo.annotation)
        if nested is None:
            continue
        seen_fields.add(top)
        model, is_list = nested
        msg += " " + _nested_shape_hint(top, model, is_list)
    if tool_name == "submit_plan":
        msg += ' steps must be a JSON array: {"steps": [{"title": "..."}]}.'
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
        scope_guard: ContractScopeGuard | None = None,
        on_tool_success: Callable[[str], None] | None = None,
        starter_kit: str | None = None,
        workflow_events: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._scope = scope
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

    def tool_scope_for_call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Return the effective pre-execution scope for this concrete tool call.

        Some tools write through the sandbox but optionally perform host-side HTTP
        before that write. The confirmation gate calls this before execute(), so a
        tool may expose a side-effect-free ``execution_scope(args)`` hook that
        depends only on already-supplied arguments and saved config. Validation
        failures fall back to the static scope; execute() still owns the real
        invalid-arguments response.
        """
        for tool in self._registry.in_scope(self._scope):
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
                scope = hook(args)
            except Exception:
                return tool.definition.runs_in
            return (
                scope
                if isinstance(scope, str) and scope in {"sandbox", "in_process", "unknown"}
                else "unknown"
            )
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

    def known_tool_names_for_requery(self) -> frozenset[str]:
        """Tool names the loop should treat as real before issuing unknown-tool hints."""

        return self.callable_tool_names()

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
                self._unknown_tool_message(call.tool_name, available),
            )

        # 1.4 CONTRACT-ENFORCE — per-phase contract scope. When a BuildContract governs
        # this run, the guard hard-denies a tool that is out of the CURRENT phase's
        # allowlist BEFORE it executes (e.g. raw file_write during an appkit EDIT phase,
        # which must use the semantic app_* tools — raw rewrite is repair-only). A
        # recoverable failure (not a crash): the model is told what is permitted here so
        # it can re-issue with an in-contract tool. No guard ⇒ no enforcement.
        if self._scope_guard is not None:
            # metadata-driven: a non-read_only tool is a mutator and is gated whatever
            # its name (no bypass via an unlisted writer/exec).
            _scope_decision = self._scope_guard.check(
                call.tool_name, is_mutating=not tool.definition.read_only
            )
            if not _scope_decision.allowed:
                # 'denied' is the policy-denial kind; the message names the contract
                # phase + what is permitted here so the model can re-issue in-scope.
                return self._fail(call, "denied", _scope_decision.reason)

        # 1.5 K1 executor-boundary elision guard (generic; ALL tools / ALL paths).
        # A weak model can COPY the `_snip_args` placeholder it sees in its own
        # action history (`<N chars elided — re-issue the call or file_read …>`)
        # back into a REAL tool argument — e.g. file_replace_lines.new_text. The
        # Observer runs the same find_elided_arg_markers() check before it reaches
        # here, but NOT every execution path goes through the Observer; this is the
        # universal chokepoint every tool call funnels through, so guarding HERE
        # makes the placeholder unable to mutate disk on ANY path (file_write,
        # file_replace_lines, any future mutator, or a direct executor call). The
        # detector anchors on the marker's STRUCTURE (count anchor or its signature
        # tail prose), so a legitimate arg that merely mentions "elided" is NOT
        # rejected. Returns a RECOVERABLE failure (invalid_arguments) — not a
        # success, finish, or planning change — so the model can resend the FULL
        # content; it never silently overwrites real content with the placeholder.
        _elided = find_elided_arg_markers(call.arguments)
        if _elided:
            return self._fail(
                call,
                "invalid_arguments",
                f"argument(s) {_elided} contain the elision placeholder text "
                "('<N chars elided …>' / 'do not copy this placeholder into a tool "
                "argument') instead of real content — this call was NOT executed. "
                "Re-issue the call with the FULL content, or file_read the path "
                "first to recover the current content, then resend.",
            )

        # 2. validate args (invalid_arguments with schema; never coerce/execute)
        arguments = normalize_list_item_wrappers(
            call.tool_name,
            tool.definition.args_model,
            call.arguments,
        )
        try:
            args = tool.definition.args_model.model_validate(arguments)
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
                    arguments if isinstance(arguments, dict) else {},
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

        # CONTRACT-ACTIVATE: advance the build-phase tracker on a successful call
        # (best-effort — a tracker error must never fail an otherwise-good tool run).
        if outcome.success and self._on_tool_success is not None:
            try:
                self._on_tool_success(call.tool_name)
            except Exception:  # noqa: BLE001 — tracking is advisory, never fatal
                pass

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
            starter_kit=self._starter_kit,
            workflow_events=self._workflow_events,
            scope_allowed_tools=self._scope.allowed_tools,
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

    def _unknown_tool_message(self, tool_name: str, available: list[str]) -> str:
        return f"unknown or out-of-scope tool {tool_name!r}; available: {available}"


def validate_args(tool_def: ToolDef, arguments: dict[str, Any]) -> BaseModel:
    """[CONTRACT helper] Validate raw arguments against a tool's args_model.
    Raises ValidationError; the executor catches it for the repair loop (§3)."""
    return tool_def.args_model.model_validate(
        normalize_list_item_wrappers(tool_def.name, tool_def.args_model, arguments)
    )
