"""Admission of a proposed tool call, and the ToolResult shapes it produces.

Two responsibilities, both pure:

* **Admission** — the ordered ladder that decides whether a proposed call may
  reach a tool at all (revoked executor, unknown/out-of-scope name, contract
  phase denial, elision placeholder, argument validation). Every rejection is a
  finished, model-readable ``ToolResult``; nothing here raises to the loop.
* **Result shapes** — the failure and success ``ToolResult`` constructors and
  the effect-receipt trust downgrade.

The functions take exactly the values they need. The one polymorphic seam the
executor owns — the unknown-tool message, which `ScopedPhaseExecutor` and
`AppKitToolExecutor` override — is passed in as a typed callable so a subclass's
override is still what the model sees.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from disco.core import ToolCall, ToolResult
from disco.core.contract import ContractScopeGuard
from disco.core.effects import (
    ActionProfile,
    EffectReceipt,
    downgrade_effect_receipts,
    validate_effect_receipts,
)
from disco.core.events import find_elided_arg_markers
from pydantic import BaseModel, ValidationError

from ..anatomy import Tool, ToolDef, ToolExecutionError, ToolOutcome
from ..registry import ToolRegistry, ToolScope
from ..sandbox.base import SandboxError
from ..secrets import CapabilityDenied
from .arguments import normalize_list_item_wrappers
from .validation_message import describe_validation_failure


class ExecutionSuperseded(RuntimeError):
    """A newer durable model view won before a tool crossed its effect boundary."""


@dataclass(frozen=True)
class AdmittedCall:
    """A proposed call that passed every pre-execution gate."""

    tool: Tool
    args: BaseModel


@dataclass(frozen=True)
class InvocationFailure:
    """A mapped failure boundary for a tool that started but did not return."""

    kind: str
    message: str
    advances_mutation_epoch: bool


def fail_result(
    call: ToolCall,
    kind: Any,
    message: str,
    *,
    validation_errors: list[dict[str, Any]] | None = None,
    expected_schema: dict[str, Any] | None = None,
    action_profile: ActionProfile | None = None,
) -> ToolResult:
    """The structured, model-readable failure the loop observes."""
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
        action_profile=action_profile,
    )


def success_result(
    call: ToolCall,
    outcome: ToolOutcome,
    *,
    effect_receipts: tuple[EffectReceipt, ...],
    action_profile: ActionProfile | None,
) -> ToolResult:
    """The result of a tool that returned, successfully or not."""
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        success=outcome.success,
        content=outcome.content,
        structured=outcome.structured,
        # DEFECT-2: tools report failure via content, not error; fall back so
        # AgentErrorEvent receives the diagnosis instead of bare "tool failed".
        error=outcome.error or (None if outcome.success else outcome.content),
        effect_receipts=effect_receipts,
        # Carried through verbatim from the tool that enforced it. The loop
        # must never have to re-derive a constraint by reading refusal prose.
        runtime_constraints=outcome.runtime_constraints,
        action_profile=action_profile,
    )


def validated_receipts(
    receipts: tuple[EffectReceipt, ...],
    profile: ActionProfile | None,
    profile_error: str | None,
) -> tuple[EffectReceipt, ...]:
    if not receipts:
        return ()
    if profile is None:
        return downgrade_effect_receipts(
            receipts,
            reason=f"exact effect receipt not trusted: {profile_error or 'unknown profile'}",
        )
    try:
        return validate_effect_receipts(profile, receipts)
    except ValueError as exc:
        return downgrade_effect_receipts(
            receipts,
            reason=f"exact effect receipt not trusted: {exc}",
        )


def classify_invocation_error(
    exc: BaseException,
    *,
    tool_def: ToolDef,
    default_timeout_s: int,
) -> InvocationFailure:
    """Map an exception that escaped a tool onto its failure boundary.

    The isinstance ladder preserves the original ``except`` clause order exactly,
    including the asymmetry that a superseded execution never crossed its effect
    boundary and therefore must NOT advance the workspace mutation epoch, while
    every other failure may follow a partial mutation and must.
    """
    if isinstance(exc, ExecutionSuperseded):
        return InvocationFailure("execution_superseded", str(exc), advances_mutation_epoch=False)
    if isinstance(exc, TimeoutError):
        timeout_s = tool_def.timeout_s or default_timeout_s
        return InvocationFailure("timeout", f"exceeded {timeout_s}s", advances_mutation_epoch=True)
    if isinstance(exc, CapabilityDenied):
        return InvocationFailure(
            "denied", f"capability not granted: {exc}", advances_mutation_epoch=True
        )
    if isinstance(exc, SandboxError):
        return InvocationFailure("sandbox_error", str(exc), advances_mutation_epoch=True)
    return InvocationFailure("execution_error", str(exc), advances_mutation_epoch=True)


def admit_call(
    call: ToolCall,
    *,
    killed: bool,
    registry: ToolRegistry,
    scope: ToolScope,
    scope_guard: ContractScopeGuard | None,
    unknown_tool_message: Callable[[str, list[str]], str],
) -> AdmittedCall | ToolResult:
    """Decide whether a proposed call may reach a tool, in the original order."""
    if killed:
        return fail_result(call, "sandbox_error", "executor killed; instance revoked")

    # 1. resolve tool (unknown OR out-of-scope → unknown_tool, never execute)
    tool = registry.get(call.tool_name, scope=scope)
    if tool is None:
        available = sorted(scope.allowed_tools & registry.names())
        return fail_result(
            call,
            "unknown_tool",
            unknown_tool_message(call.tool_name, available),
        )

    # 1.4 CONTRACT-ENFORCE — per-phase contract scope. When a BuildContract governs
    # this run, the guard hard-denies a tool that is out of the CURRENT phase's
    # allowlist BEFORE it executes (e.g. raw file_write during an appkit EDIT phase,
    # which must use the semantic app_* tools — raw rewrite is repair-only). A
    # recoverable failure (not a crash): the model is told what is permitted here so
    # it can re-issue with an in-contract tool. No guard ⇒ no enforcement.
    if scope_guard is not None:
        # metadata-driven: a non-read_only tool is a mutator and is gated whatever
        # its name (no bypass via an unlisted writer/exec).
        _scope_decision = scope_guard.check(
            call.tool_name, is_mutating=not tool.definition.read_only
        )
        if not _scope_decision.allowed:
            # 'denied' is the policy-denial kind; the message names the contract
            # phase + what is permitted here so the model can re-issue in-scope.
            return fail_result(call, "denied", _scope_decision.reason)

    # 1.5 K1 executor-boundary elision guard (generic; ALL tools / ALL paths).
    # A weak model can COPY the `_snip_args` placeholder it sees in its own
    # action history (`[[DISCO-ELIDED: N chars ...]]`)
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
        return fail_result(
            call,
            "invalid_arguments",
            f"argument(s) {_elided} contain the elision placeholder text "
            "('[[DISCO-ELIDED: ...]]') instead of real content — this call "
            "was NOT executed. Re-issue the call with the FULL real content, "
            "or file_read the path first to recover the current content, then "
            "resend.",
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
        # ErrorDetails -> plain dict, MINUS the raw `input` value. `hide_input_in_errors`
        # only affects `str(e)`; `.errors()` still carries the rejected input, which a
        # value-guard (e.g. a release argv/URL/path token) could make a secret. The
        # surfaced `validation_errors`/`content` must never echo a rejected value
        # (WO-C5 crit 3), and no consumer reads the input back, so it is dropped here.
        errors = [{k: v for k, v in err.items() if k != "input"} for err in e.errors()]
        return fail_result(
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

    return AdmittedCall(tool=tool, args=args)
